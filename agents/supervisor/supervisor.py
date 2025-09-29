from typing import Any
import json

from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
import structlog
from agents.base.base import BaseAgent, StatelessSubAgent
from agents.base.model import (
    TaskRequest,
    TaskResponse,
    TaskStatus,
)
from agents.supervisor.decomposer import TaskDecomposer
from agents.supervisor.orchestrator import OrchestrationEngine


logger = structlog.get_logger()


class SupervisorAgent(BaseAgent):
    """Primary agent that maintains context and orchestrates subagents"""

    def __init__(
        self,
        name: str,
        model: Any,
        subagents: dict[str, StatelessSubAgent] | None = None
    ):
        super().__init__(name, model)
        self.subagents = subagents or {}
        self.orchestrator = OrchestrationEngine()
        self.decomposer = TaskDecomposer()
        self.state_graph = self._build_state_graph()

    def _build_state_graph(self) -> StateGraph:
        """Build LangGraph state machine for supervision flow"""
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
        """Analyze user request and determine approach"""
        user_request = state["user_request"]

        analysis_prompt = f"""
You are an extremely correct and diligent task analysis agent.
Analyze the complexity of this user request and determine the best approach to fulfill it.

Request: {user_request}
Data: ```{json.dumps(state['request_data'], indent=2) if state.get('request_data') else "None"}```

Consider:
1. Can this be done by a single agent or needs multiple?
2. Are subtasks independent (parallel) or dependent (sequential)?
3. Does this need consensus from multiple agents?
4. Is this a large dataset needing map-reduce?

You MUST respond ONLY with a structured JSON response with your results.
Do NOT include any comments, newlines or trailing commas in the response JSON.
Do NOT include any explanations outside the JSON, your response will be parsed programmatically with Python `json.loads()`.
JSON response must not be pretty formatted.

Format your response strictly as follows:
{{
    "complexity": "low|medium|high",
    "requires_multiple_agents": true/false,
    "preferred_strategy": "sequential|parallel|consensus",
    "key_factors": ["list of key considerations"]
}}
"""

        logger.info("analyzing_request", prompt=analysis_prompt)

        response = await self.model.ainvoke([HumanMessage(content=analysis_prompt)])

        logger.info("request_analysis", response=response.content)

        analysis = json.loads(response.content)
        state["analysis"] = analysis

        return state

    async def decompose_task(self, state: dict[str, Any]) -> dict[str, Any]:
        """Decompose complex task into subtasks"""
        strategy, tasks = await self.decomposer.decompose(
            llm=self.model,
            objective=state["user_request"],
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
        """Execute tasks using appropriate strategy"""
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
        """Combine results from all subagents"""
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
            synthesis_prompt = f"""Synthesize these results into a coherent response:

Results: {json.dumps(results, indent=2)}

Original request: {state['user_request']}

Provide a comprehensive summary that addresses the original request."""

            synthesis = await self.model.ainvoke([HumanMessage(content=synthesis_prompt)])
            state["final_response"] = synthesis.content
        elif results:
            state["final_response"] = results[0]
        else:
            state["final_response"] = "Unable to complete the requested task."

        # calculate metrics
        total_time = sum(r.processing_time_ms for r in responses)
        total_tokens = sum(r.tokens_used for r in responses)
        total_cost = sum(r.cost for r in responses)

        state["execution_metrics"] = {
            "total_time_ms": total_time,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "task_count": len(responses)
        }

        return state

    async def execute(self, request: TaskRequest) -> TaskResponse:
        """Execute complete supervision flow"""
        trace = self._start_trace(request.task_id)

        # run through state graph
        initial_state = {
            "user_request": request.objective,
            "request_data": request.data,
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
                "response": final_state.get("final_response"),
                "metrics": final_state.get("execution_metrics")
            },
            metadata={
                "strategy": final_state.get("execution_strategy"),
                "task_count": len(final_state.get("tasks", []))
            }
        )

        self._end_trace(trace, TaskStatus.COMPLETE)
        return response
