from typing import Any, Callable
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
        # execute independent tasks in parallel using a worker pool pattern

        if not tasks:
            return []

        # group tasks by dependency level for batched parallel execution
        dependency_levels = self._group_by_dependency_level(tasks)
        logger.info("dependency_levels_identified", levels=len(dependency_levels))

        all_responses = []
        completed_results = {}

        async def run_task(task: TaskRequest) -> tuple[TaskRequest, TaskResponse]:
            # wrapper to return both task and response for easier tracking
            agent = self._select_agent(task, agents)
            response = await agent.execute(task)
            return task, response

        # process each dependency level
        for level_idx, level_tasks in enumerate(dependency_levels):
            logger.info(
                "processing_dependency_level",
                level=level_idx,
                tasks=len(level_tasks)
            )

            # prepare tasks with dependency results
            prepared_tasks = []
            for task in level_tasks:
                dependencies = task.constraints.get("dependencies", [])

                if dependencies and completed_results:
                    dependency_data = {
                        dep: completed_results.get(dep)
                        for dep in dependencies
                        if dep in completed_results
                    }

                    missing_deps = [dep for dep in dependencies if dep not in dependency_data]
                    if missing_deps:
                        logger.warning(
                            "missing_dependency_data",
                            task_id=task.task_id,
                            missing=missing_deps
                        )
                        # create failed response for this task
                        all_responses.append(TaskResponse(
                            task_id=task.task_id,
                            status=TaskStatus.FAILED,
                            error=f"Missing dependency data: {missing_deps}",
                            error_type="DependencyError"
                        ))
                        continue

                    task.data = task.data or {}
                    task.data["dependency_results"] = dependency_data

                prepared_tasks.append(task)

            if not prepared_tasks:
                logger.warning("no_tasks_to_execute_in_level", level=level_idx)
                continue

            # execute tasks using worker pool
            level_responses = await self._execute_with_worker_pool(prepared_tasks, run_task)

            # update completed results for the next level
            for task, response in level_responses:
                if response.status == TaskStatus.COMPLETE:
                    completed_results[task.objective] = response.result
                all_responses.append(response)

        return all_responses

    async def _execute_with_worker_pool(
        self,
        tasks: list[TaskRequest],
        task_executor: Callable,
    ) -> list[tuple[TaskRequest, TaskResponse]]:
        # execute tasks using a worker pool that starts new tasks as workers become available

        if not tasks:
            return []

        # create a queue of pending tasks
        task_queue = asyncio.Queue()
        for task in tasks:
            await task_queue.put(task)

        # results storage (preserves order)
        results = {}
        results_lock = asyncio.Lock()

        # track active workers for proper shutdown
        active_workers = []

        async def worker(worker_id: int):
            # worker coroutine that processes tasks from the queue
            while True:
                try:
                    # get next task from queue (non-blocking if queue is empty)
                    task = await asyncio.wait_for(task_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    # no more tasks available
                    break
                except Exception as e:
                    logger.error("worker_queue_error", worker_id=worker_id, error=str(e))
                    break

                try:
                    # execute the task
                    task_obj, response = await task_executor(task)

                    # store the result
                    async with results_lock:
                        results[task_obj.task_id] = (task_obj, response)

                except Exception as e:
                    logger.error(
                        "worker_task_failed",
                        worker_id=worker_id,
                        task_id=task.task_id,
                        error=str(e),
                        error_type=type(e).__name__
                    )

                    # store error response
                    async with results_lock:
                        results[task.task_id] = (
                            task,
                            TaskResponse(
                                task_id=task.task_id,
                                status=TaskStatus.FAILED,
                                error=str(e),
                                error_type=type(e).__name__
                            )
                        )
                finally:
                    task_queue.task_done()

            logger.debug("worker_finished", worker_id=worker_id)

        # create worker pool
        num_workers = min(self.max_parallel_tasks, len(tasks))
        logger.info("starting_worker_pool", num_workers=num_workers, total_tasks=len(tasks))

        workers = [
            asyncio.create_task(worker(i))
            for i in range(num_workers)
        ]
        active_workers.extend(workers)

        # wait for all workers to complete
        try:
            await asyncio.gather(*workers, return_exceptions=False)
        except Exception as e:
            logger.error("worker_pool_error", error=str(e))
            # cancel remaining workers
            for w in workers:
                if not w.done():
                    w.cancel()
            raise
        finally:
            # ensure all tasks were processed
            await task_queue.join()

        # return results in original task order
        ordered_results = []
        for task in tasks:
            if task.task_id in results:
                ordered_results.append(results[task.task_id])
            else:
                logger.error("missing_task_result", task_id=task.task_id)
                # add a failure response for missing results
                ordered_results.append((
                    task,
                    TaskResponse(
                        task_id=task.task_id,
                        status=TaskStatus.FAILED,
                        error="Task result not found after execution",
                        error_type="MissingResultError"
                    )
                ))

        return ordered_results

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
        # execute tasks using specified strategy

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
