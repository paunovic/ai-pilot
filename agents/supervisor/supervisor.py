from typing import Any
import json

from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from agents.base.model import AgentCapability
import structlog
from agents.base.base import BaseAgent, StatelessSubAgent
from agents.base.model import (
    TaskRequest,
    TaskResponse,
    TaskStatus,
)
from agents.supervisor.model import (
    TaskComplexityAnalysis,
)
from agents.supervisor.decomposer import TaskDecomposer
from agents.supervisor.orchestrator import OrchestrationEngine
from agents.utils import (
    calculate_token_usage_cost,
    extract_token_usage,
)


logger = structlog.get_logger()


class SupervisorAgent(BaseAgent):
    """Primary agent that maintains context and orchestrates subagents"""

    def __init__(
        self,
        name: str,
        llm: Any,
        subagents: dict[str, StatelessSubAgent] | None = None
    ):
        super().__init__(name=name, capability=AgentCapability.SUPERVISOR, llm=llm, model_name=llm.model)

        self.subagents = subagents or {}
        self.orchestrator = OrchestrationEngine()
        self.decomposer = TaskDecomposer()
        self.state_graph = self._build_state_graph()

    def _build_state_graph(self) -> StateGraph:
        # build langgraph state machine for supervision
        workflow = StateGraph(dict)

        # define nodes
        workflow.add_node("analyze", self.analyze_request)
        workflow.add_node("decompose", self.decompose_task)
        workflow.add_node("orchestrate", self.orchestrate_execution)
        workflow.add_node("synthesize", self.synthesize_results)

        # define edges
        workflow.set_entry_point("analyze")
        workflow.add_edge("analyze", "decompose")
        workflow.add_edge("decompose", "orchestrate")
        workflow.add_edge("orchestrate", "synthesize")
        workflow.add_edge("synthesize", END)

        return workflow.compile(checkpointer=MemorySaver())

    async def analyze_request(self, state: dict[str, Any]) -> dict[str, Any]:
        # analyze user request complexity and determine execution strategy

        user_request = state["user_request"]

        analysis_prompt = f"""
You are an extremely correct and diligent task analysis agent.
Analyze the complexity of this user request and determine the best approach to fulfill it.

Request: {user_request}
Data: ```{json.dumps(state['request_data'], indent=2) if state.get('request_data') else "None"}```

Consider:
1. Can this be done by a single agent or needs multiple?
2. Are subtasks independent (parallel), dependent (sequential), or require consensus (consensus)?
3. Does this need consensus from multiple agents?

IMPORTANT: Your response is parsed with `llm.with_structured_output()` so you MUST respond ONLY with a structured JSON response that is compatible with Pydantic.
"""

        logger.info("analyzing_request", prompt=analysis_prompt)

        response = await (
            self.llm
            .with_structured_output(TaskComplexityAnalysis, include_raw=True)
            .ainvoke([HumanMessage(content=analysis_prompt)])
        )

        logger.debug("request_analysis", response=response)

        token_usage = extract_token_usage(response["raw"])
        cost = calculate_token_usage_cost(
            token_usage["prompt_tokens"],
            token_usage["completion_tokens"],
            self.model_name,
        )

        state["execution_metrics"]["supervisor"]["analysis_tokens"] = state["execution_metrics"]["supervisor"]["analysis_tokens"] + token_usage["total_tokens"]
        state["execution_metrics"]["supervisor"]["analysis_cost"] = state["execution_metrics"]["supervisor"]["analysis_cost"] + cost

        state["analysis"] = response["parsed"]

        logger.info("analysis_complete", analysis=state["analysis"].model_dump())

        return state

    async def decompose_task(self, state: dict[str, Any]) -> dict[str, Any]:
        # decompose complex task into subtasks

        strategy, tasks = await self.decomposer.decompose(
            llm=self.llm,
            objective=state["user_request"],
            state=state,
            data=state["request_data"],
            analysis=state["analysis"],
        )

        state["execution_strategy"] = strategy
        state["tasks"] = tasks

        logger.info(
            "task_decomposed",
            strategy=strategy,
            task_count=len(tasks)
        )

        return state

    async def orchestrate_execution(self, state: dict[str, Any]) -> dict[str, Any]:
        # execute tasks using appropriate strategy

        strategy = state["execution_strategy"]
        tasks = state["tasks"]

        responses = await self.orchestrator.execute_with_strategy(
            strategy,
            tasks,
            self.subagents
        )

        state["task_responses"] = responses

        # log execution summary
        successful = sum(1 for r in responses if r.status == TaskStatus.COMPLETE)
        failed = sum(1 for r in responses if r.status == TaskStatus.FAILED)

        logger.info(
            "orchestration_complete",
            total_tasks=len(tasks),
            successful=successful,
            failed=failed
        )

        return state

    async def synthesize_results(self, state: dict[str, Any]) -> dict[str, Any]:
        # synthesize results from multiple tasks into final response

        responses = state["task_responses"]

        # extract successful results
        results = []
        for response in responses:
            if response.status == TaskStatus.COMPLETE and response.result:
                results.append(response.result)
            elif response.partial_result:
                results.append(response.partial_result)

        # use LLM to synthesize if multiple results
        if len(results) > 1:
            synthesis_prompt = f"""
You are extremely correct and diligent at synthesizing information from multiple sources into a coherent, concise summary.

Results: {json.dumps(results, indent=2)}

Original request: {state['user_request']}

Provide a comprehensive summary that addresses the original request.
"""

            synthesis_response = await self.llm.ainvoke([HumanMessage(content=synthesis_prompt)])
            state["final_response"] = synthesis_response.content

            token_usage = extract_token_usage(synthesis_response)
            cost = calculate_token_usage_cost(
                token_usage["prompt_tokens"],
                token_usage["completion_tokens"],
                self.model_name,
            )

            state["execution_metrics"]["supervisor"]["synthesis_tokens"] = state["execution_metrics"]["supervisor"]["synthesis_tokens"] + token_usage["total_tokens"]
            state["execution_metrics"]["supervisor"]["synthesis_cost"] = state["execution_metrics"]["supervisor"]["synthesis_cost"] + cost

        elif results:
            state["final_response"] = results[0]
        else:
            state["final_response"] = "Unable to complete the requested task."

        # calculate metrics
        total_time = sum(r.processing_time_ms for r in responses)
        total_tokens = sum(r.tokens_used for r in responses)
        total_cost = sum(r.cost for r in responses)

        state["execution_metrics"]["total_time_ms"] = total_time
        state["execution_metrics"]["task_tokens"] = total_tokens
        state["execution_metrics"]["task_cost"] = total_cost
        state["execution_metrics"]["task_count"] = len(responses)

        return state

    async def execute(self, request: TaskRequest) -> TaskResponse:
        # execute supervisor task

        trace = self._start_trace(request.task_id)

        supervisor_tokens = 0
        supervisor_cost = 0.0

        try:
            # run through state graph
            initial_state = {
                "user_request": request.objective,
                "request_data": request.data,
                "execution_metrics": {
                    "supervisor": {
                        "analysis_tokens": 0,
                        "analysis_cost": 0.0,
                        "decomposition_tokens": 0,
                        "decomposition_cost": 0.0,
                        "orchestration_tokens": 0,
                        "orchestration_cost": 0.0,
                        "synthesis_tokens": 0,
                        "synthesis_cost": 0.0,
                    },
                },
            }

            final_state = await self.state_graph.ainvoke(
                initial_state,
                config={"configurable": {"thread_id": request.task_id}}
            )

            # build response
            response = TaskResponse(
                task_id=request.task_id,
                status=TaskStatus.COMPLETE,
                result={
                    "response": final_state["final_response"],
                    "execution_metrics": final_state["execution_metrics"],
                },
                metadata={
                    "strategy": final_state["execution_strategy"],
                    "task_count": len(final_state["tasks"]),
                }
            )

            supervisor_tokens = (
                final_state["execution_metrics"]["supervisor"]["analysis_tokens"]
                + final_state["execution_metrics"]["supervisor"]["decomposition_tokens"]
                + final_state["execution_metrics"]["supervisor"]["orchestration_tokens"]
                + final_state["execution_metrics"]["supervisor"]["synthesis_tokens"]
            )
            supervisor_cost = (
                final_state["execution_metrics"]["supervisor"]["analysis_cost"]
                + final_state["execution_metrics"]["supervisor"]["decomposition_cost"]
                + final_state["execution_metrics"]["supervisor"]["orchestration_cost"]
                + final_state["execution_metrics"]["supervisor"]["synthesis_cost"]
            )

            self._end_trace(trace, TaskStatus.COMPLETE, supervisor_tokens, supervisor_cost)
            return response
        except Exception as e:
            logger.error("supervisor_execution_failed", error=str(e))
            self._end_trace(trace, TaskStatus.FAILED, supervisor_tokens, supervisor_cost, str(e))
            raise
