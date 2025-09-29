from typing import Any
import asyncio
from concurrent.futures import ThreadPoolExecutor

import structlog

from agents.base.base import StatelessSubAgent
from agents.base.model import (
    TaskRequest,
    TaskResponse,
    TaskStatus,
    ExecutionStrategy,
)

logger = structlog.get_logger()


class OrchestrationEngine:
    """Manages parallel and sequential execution of agents"""

    def __init__(self, max_parallel: int = 10):
        self.max_parallel = max_parallel
        self.execution_history: list[dict[str, Any]] = []

    async def __aenter__(self):
        self.executor = ThreadPoolExecutor(max_workers=self.max_parallel)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.executor.shutdown(wait=True)

    async def execute_sequential(
        self,
        tasks: list[TaskRequest],
        agents: dict[str, StatelessSubAgent]
    ) -> list[TaskResponse]:
        """Execute tasks sequentially, respecting dependencies"""
        responses = []
        completed_tasks = set()
        task_results = {}  # store results for dependent tasks

        for task in tasks:
            # check if dependencies are satisfied
            dependencies = task.constraints.get("dependencies", [])
            unsatisfied_deps = [dep for dep in dependencies if dep not in completed_tasks]

            if unsatisfied_deps:
                logger.warning(
                    "dependency_not_satisfied",
                    task_id=task.task_id,
                    missing=unsatisfied_deps
                )
                # could implement waiting/retry logic here

            # add results from previous tasks to current task's data
            if dependencies and task_results:
                dependency_data = {
                    dep: task_results.get(dep)
                    for dep in dependencies
                    if dep in task_results
                }
                if task.data is None:
                    task.data = {}
                task.data["dependency_results"] = dependency_data

            agent = self._select_agent(task, agents)
            response = await agent.execute(task)
            responses.append(response)

            # track completion and store results
            if response.status == TaskStatus.COMPLETE:
                completed_tasks.add(task.objective)
                task_results[task.objective] = response.result

            logger.info(
                "sequential_task_complete",
                task_id=task.task_id,
                status=response.status,
                agent=agent.name,
                dependencies_satisfied=len(dependencies) - len(unsatisfied_deps)
            )

            # if task failed, halt further execution
            if response.status == TaskStatus.FAILED:
                logger.error(
                    "sequential_execution_halted",
                    task_id=task.task_id,
                    reason=response.error
                )
                break

        return responses

    async def execute_parallel(
        self,
        tasks: list[TaskRequest],
        agents: dict[str, StatelessSubAgent]
    ) -> list[TaskResponse]:
        """Execute tasks in parallel, handling dependencies through batching"""
        if not tasks:
            return []

        # group tasks by dependency level for batched parallel execution
        dependency_levels = self._group_by_dependency_level(tasks)
        all_responses = []
        completed_results = {}

        async def run_task(task: TaskRequest) -> TaskResponse:
            agent = self._select_agent(task, agents)
            return await agent.execute(task)

        for level_tasks in dependency_levels:
            logger.info("executing_parallel_batch", batch_size=len(level_tasks))

            # add dependency results to each task in this level
            for task in level_tasks:
                dependencies = task.constraints.get("dependencies", [])
                if dependencies and completed_results:
                    dependency_data = {
                        dep: completed_results.get(dep)
                        for dep in dependencies
                        if dep in completed_results
                    }
                    if task.data is None:
                        task.data = {}
                    task.data["dependency_results"] = dependency_data

            # create coroutines for current level
            coroutines = [run_task(task) for task in level_tasks]

            # execute in parallel with controlled concurrency
            batch_responses = []
            for i in range(0, len(coroutines), self.max_parallel):
                batch = coroutines[i:i + self.max_parallel]
                batch_results = await asyncio.gather(*batch, return_exceptions=True)

                for j, result in enumerate(batch_results):
                    if isinstance(result, Exception):
                        task_idx = i + j
                        batch_responses.append(TaskResponse(
                            task_id=level_tasks[task_idx].task_id,
                            status=TaskStatus.FAILED,
                            error=str(result),
                            error_type=type(result).__name__
                        ))
                    else:
                        batch_responses.append(result)

            # update completed results for next level
            for task, response in zip(level_tasks, batch_responses):
                if response.status == TaskStatus.COMPLETE:
                    completed_results[task.objective] = response.result

            all_responses.extend(batch_responses)

        return all_responses

    async def execute_consensus(
        self,
        task: TaskRequest,
        agents: list[StatelessSubAgent],
        min_agreement: float = 0.66
    ) -> TaskResponse:
        """Execute consensus pattern - multiple agents vote on result"""
        # run same task through multiple agents
        responses = await asyncio.gather(*[
            agent.execute(task) for agent in agents
        ])

        # analyze consensus
        successful = [r for r in responses if r.status == TaskStatus.COMPLETE]

        if not successful:
            return TaskResponse(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                error="No agents succeeded",
                metadata={"attempted_agents": len(agents)}
            )

        # simple voting mechanism (in production, use more sophisticated consensus)
        # results = [r.result for r in successful if r.result]

        # for demo, just return the most confident result
        best_response = max(successful, key=lambda r: r.confidence)
        best_response.metadata["consensus"] = {
            "total_agents": len(agents),
            "successful_agents": len(successful),
            "confidence_scores": [r.confidence for r in successful]
        }

        return best_response

    def _group_by_dependency_level(self, tasks: list[TaskRequest]) -> list[list[TaskRequest]]:
        """Group tasks by dependency level for batched execution"""
        # build dependency graph
        task_deps = {}
        task_map = {}

        for task in tasks:
            task_deps[task.objective] = task.constraints.get("dependencies", [])
            task_map[task.objective] = task

        # calculate dependency levels
        levels = []
        remaining_tasks = set(task.objective for task in tasks)
        completed = set()

        while remaining_tasks:
            # find tasks with no unmet dependencies
            current_level = []
            for task_obj in list(remaining_tasks):
                deps = task_deps[task_obj]
                if all(dep in completed for dep in deps):
                    current_level.append(task_map[task_obj])
                    remaining_tasks.remove(task_obj)
                    completed.add(task_obj)

            if not current_level:
                # circular dependency or missing dependency - add remaining tasks
                logger.warning("dependency_resolution_failed", remaining=list(remaining_tasks))
                current_level = [task_map[obj] for obj in remaining_tasks]
                remaining_tasks.clear()

            levels.append(current_level)

        return levels

    def _select_agent(
        self,
        task: TaskRequest,
        agents: dict[str, StatelessSubAgent]
    ) -> StatelessSubAgent:
        """Select appropriate agent for task"""
        # match by capability
        for agent in agents.values():
            if agent.capability and agent.capability.value == task.task_type:
                return agent

        # fallback to first available
        return next(iter(agents.values()))

    async def execute_with_strategy(
        self,
        strategy: ExecutionStrategy,
        tasks: list[TaskRequest],
        agents: dict[str, StatelessSubAgent]
    ) -> list[TaskResponse]:
        """Execute tasks using specified strategy"""

        logger.info("executing_strategy", strategy=strategy, task_count=len(tasks))

        if strategy == ExecutionStrategy.SEQUENTIAL:
            return await self.execute_sequential(tasks, agents)
        elif strategy == ExecutionStrategy.PARALLEL:
            return await self.execute_parallel(tasks, agents)
        elif strategy == ExecutionStrategy.CONSENSUS:
            # run first task through multiple agents
            if tasks:
                agent_list = list(agents.values())[:3]  # use up to 3 agents
                result = await self.execute_consensus(tasks[0], agent_list)
                return [result]
            return []
        else:
            raise Exception(f"Unsupported strategy: {strategy}")
