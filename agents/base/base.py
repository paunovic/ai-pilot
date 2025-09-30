from abc import ABC, abstractmethod
import arrow
import json
import time
import traceback
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
from agents.utils import (
    calculate_token_usage_cost,
    extract_token_usage,
)


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
            logger.info("subagent_prompt", agent=self.name, prompt=prompt)

            # execute
            start_time: float = time.time()
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])

            processing_time: float = int((time.time() - start_time) * 1000)

            logger.debug("subagent_llm_response", agent=self.name, response=response)

            confidence: float | None = None
            confidence_reasoning: str | None = None

            # handle both structured and unstructured responses
            if isinstance(response, dict) and "parsed" in response:
                # response from llm.with_structured_output(include_raw=True)
                parsed_content = response["parsed"]
                raw_response = response["raw"]

                # extract confidence from parsed Pydantic model
                if hasattr(parsed_content, "confidence"):
                    confidence = parsed_content.confidence
                    confidence_reasoning = getattr(parsed_content, "confidence_reasoning", None)
                    # convert Pydantic model to dict
                    result = parsed_content.model_dump()
                else:
                    result = parsed_content.model_dump() if hasattr(parsed_content, 'model_dump') else parsed_content
            elif isinstance(response, AIMessage):
                # response without include_raw=True or plain AIMessage
                raw_response = response
                try:
                    parsed_content = json.loads(response.content)
                    confidence = parsed_content.get("confidence", None)
                    confidence_reasoning = parsed_content.get("confidence_reasoning", None)
                    result = parsed_content
                except (json.JSONDecodeError, AttributeError):
                    # fallback for non-JSON responses
                    result = {"content": response.content}
            else:
                # handle structured output without raw (response is the parsed object directly)
                raw_response = None
                if hasattr(response, "confidence"):
                    confidence = response.confidence
                    confidence_reasoning = getattr(response, "confidence_reasoning", None)
                    result = response.model_dump()
                else:
                    result = response.model_dump() if hasattr(response, "model_dump") else response

            # extract token usage from raw response if available
            if raw_response:
                token_usage: dict = extract_token_usage(raw_response)
            else:
                # fallback if no raw response available for now
                # XXX: FIXME: this will not be accurate
                token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

            # calculate cost
            cost: float = calculate_token_usage_cost(
                token_usage["prompt_tokens"],
                token_usage["completion_tokens"],
                self.model_name,
            )

            # build response
            task_response = TaskResponse(
                task_id=request.task_id,
                status=TaskStatus.COMPLETE,
                result=result,
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
            logger.info("subagent_response", agent=self.name, response=task_response.model_dump_json(), cost=cost)

            # cache result
            self.cache.set(request, task_response)

            self._end_trace(trace, TaskStatus.COMPLETE, token_usage["total_tokens"], cost)
            return task_response

        except Exception as e:
            logger.error("subagent_error", agent=self.name, error=str(e), traceback=traceback.format_exc())
            self._end_trace(trace, TaskStatus.FAILED, 0, 0.0, str(e))

            return TaskResponse(
                task_id=request.task_id,
                status=TaskStatus.FAILED,
                error=str(e),
                error_type=type(e).__name__,
                metadata={"agent": self.name}
            )
