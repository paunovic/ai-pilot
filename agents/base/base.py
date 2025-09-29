from abc import ABC, abstractmethod
import arrow
import json
import time
from typing import Any

from langchain_core.messages import HumanMessage
import structlog
from langchain_core.messages import AIMessage
from agents.base.cache import TaskCache
from agents.base.model import (
    TaskRequest,
    TaskResponse,
    TaskStatus,
    ExecutionTrace,
    AgentCapability,
)

import config


logger = structlog.get_logger()


class BaseAgent(ABC):
    """Abstract base class for all agents"""

    def __init__(
        self,
        name: str,
        capability: AgentCapability,
        llm: Any,
        model_name: str,
    ):
        self.name = name
        self.capability = capability
        self.llm = llm
        self.model_name = model_name
        self.execution_traces: list[ExecutionTrace] = []

    @abstractmethod
    async def execute(self, request: TaskRequest) -> TaskResponse:
        # abstract method to execute a task and return a structured response
        pass

    def _start_trace(self, task_id: str) -> ExecutionTrace:
        # start execution trace
        trace = ExecutionTrace(
            agent_name=self.name,
            task_id=task_id,
            start_time=arrow.utcnow(),
            status=TaskStatus.RUNNING,
        )
        self.execution_traces.append(trace)
        return trace

    def _end_trace(
        self,
        trace: ExecutionTrace,
        status: TaskStatus,
        tokens: int = 0,
        cost: float = 0.0,
        error: str | None = None
    ):
        # end execution trace
        trace.end_time = arrow.utcnow()
        trace.status = status
        trace.tokens_used = tokens
        trace.cost = cost
        trace.error = error

    def _calculate_cost(self, input_tokens: int, output_tokens: int, model_name: str) -> float:
        # calculate cost based on token usage and model pricing
        model_pricing = config.MODEL_PRICING[model_name]
        input_cost = (input_tokens / 1000) * model_pricing["input_cost_per_1k"]
        output_cost = (output_tokens / 1000) * model_pricing["output_cost_per_1k"]
        return input_cost + output_cost

    def _extract_token_usage(self, response) -> dict[str, int]:
        # extract token usage from LangChain response
        metadata = response.response_metadata
        usage = metadata["usage"]

        token_usage: dict = {
            "prompt_tokens": usage["input_tokens"],
            "completion_tokens": usage["output_tokens"],
            "total_tokens": usage.get(
                "total_tokens",
                usage["input_tokens"] + usage["output_tokens"],
            ),
        }

        return token_usage


class StatelessSubAgent(BaseAgent):
    """Stateless subagent - pure function execution"""

    def __init__(
        self,
        name: str,
        capability: AgentCapability,
        llm: Any,
        model_name: str,
        prompt_template: str,
    ):
        super().__init__(name=name, capability=capability, llm=llm, model_name=model_name)
        self.prompt_template = prompt_template
        self.cache = TaskCache()

    async def execute(self, request: TaskRequest) -> TaskResponse:
        # execute task in a stateless manner

        trace = self._start_trace(request.task_id)

        # check cache first
        if cached := self.cache.get(request):
            self._end_trace(trace, TaskStatus.COMPLETE, 0, 0.0)
            return cached

        try:
            # build prompt
            prompt = self.prompt_template.format(
                capability=self.capability,
                objective=request.objective,
                data=json.dumps(request.data) if request.data else None,
                constraints=json.dumps(request.constraints) if request.constraints else None,
            )
            logger.debug("subagent_prompt", agent=self.name, prompt=prompt)

            # execute
            start_time: float = time.time()
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])

            processing_time: float = int((time.time() - start_time) * 1000)

            logger.info("subagent_llm_response", agent=self.name, response=response)

            if isinstance(response, AIMessage):
                response = {
                    "raw": response,
                    "parsed": json.loads(response.content)
                }
                confidence = response["parsed"]["confidence"]
                confidence_reasoning = response["parsed"]["confidence_reasoning"]
            else:
                confidence = response["parsed"].confidence
                confidence_reasoning = response["parsed"].confidence_reasoning

            token_usage: dict = self._extract_token_usage(response["raw"])

            # calculate cost
            cost: float = self._calculate_cost(
                token_usage["prompt_tokens"],
                token_usage["completion_tokens"],
                self.model_name,
            )

            # build response
            task_response = TaskResponse(
                task_id=request.task_id,
                status=TaskStatus.COMPLETE,
                result=response["parsed"],
                processing_time_ms=processing_time,
                confidence=confidence,
                confidence_reasoning=confidence_reasoning,
                tokens_used=token_usage["total_tokens"],
                cost=cost,
                metadata={
                    "agent": self.name,
                    "capability": self.capability,
                },
            )
            logger.debug("subagent_response", agent=self.name, response=task_response.model_dump_json(), cost=cost)

            # cache result
            self.cache.set(request, task_response)

            self._end_trace(trace, TaskStatus.COMPLETE, token_usage["total_tokens"], cost)
            return task_response

        except Exception as e:
            logger.error("subagent_error", agent=self.name, error=str(e))
            self._end_trace(trace, TaskStatus.FAILED, 0, 0.0, str(e))

            return TaskResponse(
                task_id=request.task_id,
                status=TaskStatus.FAILED,
                error=str(e),
                error_type=type(e).__name__,
                metadata={"agent": self.name}
            )
