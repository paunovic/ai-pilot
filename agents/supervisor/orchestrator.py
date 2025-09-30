from typing import Any
import asyncio

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

    def __init__(self, max_parallel_tasks: int = 8):
        self.max_parallel_tasks = max_parallel_tasks
        self.execution_history: list[dict[str, Any]] = []

    async def execute_sequential(
        self,
        tasks: list[TaskRequest],
        agents: dict[str, StatelessSubAgent]
    ) -> list[TaskResponse]:
        # execute tasks sequentially, respecting dependencies

        responses = []
        completed_tasks = set()
        task_results = {}  # store results for dependent tasks

        for task in tasks:
            # check if dependencies are satisfied
            dependencies: list[str] = task.constraints.get("dependencies", [])
            unsatisfied_deps: list[str] = [dep for dep in dependencies if dep not in completed_tasks]

            # if dependencies are not met, halt further execution
            if unsatisfied_deps:
                logger.error(
                    "dependency_not_satisfied",
                    task_id=task.task_id,
                    missing=unsatisfied_deps,
                )
                break

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

            # run the task
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
                    reason=response.error,
                )
                break

        return responses

    async def execute_parallel(
        self,
        tasks: list[TaskRequest],
        agents: dict[str, StatelessSubAgent]
    ) -> list[TaskResponse]:
        # execute independent tasks in parallel, respecting dependencies

        if not tasks:
            return []

        # group tasks by dependency level for batched parallel execution
        dependency_levels = self._group_by_dependency_level(tasks)
        logger.info("dependency_levels_identified", levels=len(dependency_levels))
        all_responses = []
        completed_results = {}

        async def run_task(task: TaskRequest) -> TaskResponse:
            agent = self._select_agent(task, agents)
            return await agent.execute(task)

        for level_tasks in dependency_levels:
            logger.info("execute_parallel_tasks", tasks=len(level_tasks))

            # add dependency results to each task in this level
            task_coroutines = []
            for task in level_tasks:
                dependencies = task.constraints.get("dependencies", [])
                logger.info(f"dependencies_for_task_{task.task_id}", dependencies=dependencies)
                if dependencies and completed_results:
                    dependency_data = {
                        dep: completed_results.get(dep)
                        for dep in dependencies
                        if dep in completed_results
                    }
                    if len(dependency_data) != len(dependencies):
                        logger.warning(
                            "missing_dependency_data",
                            task_id=task.task_id,
                            missing=[dep for dep in dependencies if dep not in dependency_data]
                        )
                        break

                    task.data = task.data or {}
                    task.data["dependency_results"] = dependency_data

                task_coroutines.append(run_task(task))

            if len(task_coroutines) != len(level_tasks):
                logger.error(
                    "parallel_execution_halted",
                    reason="missing dependency data for some tasks",
                )
                break

            # execute in parallel with controlled concurrency
            level_responses = []
            for i in range(0, len(task_coroutines), self.max_parallel_tasks):
                batch = task_coroutines[i:i + self.max_parallel_tasks]
                batch_results = await asyncio.gather(*batch, return_exceptions=True)

                for j, result in enumerate(batch_results):
                    if isinstance(result, Exception):
                        task_idx = i + j
                        level_responses.append(TaskResponse(
                            task_id=level_tasks[task_idx].task_id,
                            status=TaskStatus.FAILED,
                            error=str(result),
                            error_type=type(result).__name__
                        ))
                    else:
                        level_responses.append(result)

            assert len(level_responses) == len(level_tasks), "Mismatch in task and response count"

            # update completed results for next level
            for task, response in zip(level_tasks, level_responses):
                if response.status == TaskStatus.COMPLETE:
                    completed_results[task.objective] = response.result

            all_responses.extend(level_responses)

        return all_responses

    async def execute_consensus(
        self,
        task: TaskRequest,
        agents: list[StatelessSubAgent],
    ) -> TaskResponse:
        # execute task with multiple agents and reach consensus

        # run same task through multiple agents
        responses = await asyncio.gather(*[
            agent.execute(task) for agent in agents
        ])

        # analyze consensus
        successful_responses = [r for r in responses if r.status == TaskStatus.COMPLETE]

        if not successful_responses:
            return TaskResponse(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                error="No agents succeeded",
                metadata={"attempted_agents": len(agents)},
            )

        # just return the most confident result for now
        best_response = max(successful_responses, key=lambda r: r.confidence)
        best_response.metadata["consensus"] = {
            "total_agents": len(agents),
            "successful_agents": len(successful_responses),
            "confidence_scores": [r.confidence for r in successful_responses],
        }

        return best_response

    def _group_by_dependency_level(self, tasks: list[TaskRequest]) -> list[list[TaskRequest]]:
        # group tasks by dependency level for batched execution

        # build dependency graph
        task_deps = {}
        task_map = {}

        for task in tasks:
            task_deps[task.objective] = task.constraints.get("dependencies", [])
            task_map[task.objective] = task

        logger.info("task_deps_graph", graph=task_deps)

        # calculate dependency levels
        levels = []
        assigned = set()
        while len(assigned) < len(tasks):
            current_level = []
            for obj, deps in task_deps.items():
                if obj in assigned:
                    continue
                if all(dep in assigned for dep in deps):
                    current_level.append(task_map[obj])
            if not current_level:
                # circular dependency or unresolved dependency detected
                logger.error("circular_or_unresolved_dependency", remaining_tasks=len(tasks) - len(assigned))
                break
            levels.append(current_level)
            for task in current_level:
                assigned.add(task.objective)

        logger.info("dependency_levels", levels=levels)

        return levels

    def _select_agent(
        self,
        task: TaskRequest,
        agents: dict[str, StatelessSubAgent]
    ) -> StatelessSubAgent:
        # select appropriate agent for task

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
        # eecute tasks using specified strategy

        logger.info("executing_strategy", strategy=strategy, task_count=len(tasks))

        if strategy == ExecutionStrategy.SEQUENTIAL:
            return await self.execute_sequential(tasks, agents)
        elif strategy == ExecutionStrategy.PARALLEL:
            return await self.execute_parallel(tasks, agents)
        elif strategy == ExecutionStrategy.CONSENSUS:
            if tasks:
                agent_list = list(agents.values())
                result = await self.execute_consensus(tasks[0], agent_list)
                return [result]
            return []
        else:
            raise Exception(f"Unsupported strategy: {strategy}")
