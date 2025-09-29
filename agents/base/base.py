from abc import ABC, abstractmethod
import arrow
import json
import time

from langchain_core.messages import HumanMessage
import structlog
from agents.base.cache import TaskCache
from agents.base.model import (
    TaskRequest,
    TaskResponse,
    TaskStatus,
    ExecutionTrace,
    AgentCapability,
)


logger = structlog.get_logger()


class BaseAgent(ABC):
    """Abstract base class for all agents"""

    def __init__(
        self,
        name: str,
        model,
        capability: AgentCapability | None = None
    ):
        self.name = name
        self.capability = capability
        self.model = model
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
        model,
        prompt_template: str,
    ):
        super().__init__(name, model, capability)
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
                constraints=json.dumps(request.constraints)
            )
            logger.debug("subagent_prompt", agent=self.name, prompt=prompt)

            # execute
            start_time = time.time()
            response = await self.model.ainvoke([HumanMessage(content=prompt)])

            processing_time = int((time.time() - start_time) * 1000)

            # parse response to structured format
            result = json.loads(response.content)

            # build response
            task_response = TaskResponse(
                task_id=request.task_id,
                status=TaskStatus.COMPLETE,
                result=result,
                processing_time_ms=processing_time,
                confidence=result["confidence"],
                confidence_reasoning=result.get("confidence_reasoning"),
                metadata={"agent": self.name, "capability": self.capability}
            )
            logger.debug("subagent_response", agent=self.name, response=task_response.to_json())

            # cache result
            self.cache.set(request, task_response)

            self._end_trace(trace, TaskStatus.COMPLETE, 100, 0.01)
            return task_response

        except Exception as e:
            logger.error(
                "subagent_error",
                agent=self.name,e
                rror=str(e),
                subagent_raw_response=response.content if 'response' in locals() else None,
            )
            self._end_trace(trace, TaskStatus.FAILED, 0, 0.0, str(e))

            return TaskResponse(
                task_id=request.task_id,
                status=TaskStatus.FAILED,
                error=str(e),
                error_type=type(e).__name__,
                metadata={"agent": self.name}
            )
