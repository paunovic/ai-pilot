from typing import Any
import json

from langchain_core.messages import HumanMessage
import structlog

from agents.base.model import (
    TaskRequest,
    TaskPriority,
    ExecutionStrategy,
)
from agents.supervisor.model import (
    SubtaskDecomposition,
    TaskComplexityAnalysis,
    TaskDecompositionAnalysis,
)

logger = structlog.get_logger()


class TaskDecomposer:
    """Decomposes complex tasks into subtasks"""

    @staticmethod
    async def analyze_dependencies(
        llm: Any,
        objectives: list[str],
        context: str | None = None
    ) -> tuple[ExecutionStrategy, dict[str, list[str]]]:
        """
        Use LLM to analyze task dependencies and determine execution strategy
        Returns: (strategy, dependency_graph)
        """
        if not objectives:
            return ExecutionStrategy.SEQUENTIAL, {}

        if len(objectives) == 1:
            return ExecutionStrategy.SEQUENTIAL, {objectives[0]: []}

        # build comprehensive prompt for dependency analysis
        objectives_text = "\n".join([f"{i+1}. {obj}" for i, obj in enumerate(objectives)])
        context_text = f"{context}" if context else "None"

        dependency_analysis_prompt = f"""
You are an extremely correct and diligent expert task planner. Analyze these objectives to determine dependencies and execution strategy.

Objectives to analyze:
{objectives_text}

Context:
{context_text}

Carefully consider:
1. **Sequential Dependencies**: Does one task require outputs/results from another?
2. **Data Dependencies**: Do tasks share or modify the same data?
3. **Logical Dependencies**: Must tasks happen in a specific order due to business logic?
4. **Resource Dependencies**: Do tasks compete for limited resources?
5. **Parallelization Potential**: Can independent tasks run simultaneously?

Execution Strategies:
- **sequential**: Tasks must run one after another due to dependencies
- **parallel**: Tasks are independent and can run simultaneously
- **consensus**: Same task needs multiple perspectives/validation

Rules:
- `dependency_graph` keys must exactly match the objective texts
- Empty dependency list [] means no prerequisites
- `execution_order` should reflect optimal task sequence
- `parallel_groups` groups tasks that can run simultaneously
- Be conservative with parallelization if unsure about dependencies

IMPORTANT: Your response is parsed with `llm.with_structured_output()` so you MUST respond ONLY with a structured JSON response that is compatible with Pydantic.
    """

        response = await (
            llm
            .with_structured_output(TaskDecompositionAnalysis)
            .ainvoke([HumanMessage(content=dependency_analysis_prompt)])
        )

        logger.info("dependency_analysis_response", response=response)

        # validate and extract results
        strategy = ExecutionStrategy(response.strategy)
        dependency_graph = response.dependency_graph

        # validate that all objectives are represented in dependency graph
        missing_objectives = set(objectives) - set(dependency_graph.keys())
        if missing_objectives:
            logger.warning("missing_objectives_in_graph", missing=list(missing_objectives))
            # add missing objectives with no dependencies
            for obj in missing_objectives:
                dependency_graph[obj] = []

        # validate that dependencies reference valid objectives
        valid_objectives = set(objectives)
        for obj, deps in dependency_graph.items():
            invalid_deps = [dep for dep in deps if dep not in valid_objectives]
            if invalid_deps:
                logger.warning("invalid_dependencies", objective=obj, invalid=invalid_deps)
                # remove invalid dependencies
                dependency_graph[obj] = [dep for dep in deps if dep in valid_objectives]

        # log analysis results for monitoring
        logger.info(
            "dependency_analysis_complete",
            strategy=strategy,
            total_objectives=len(objectives),
            has_dependencies=any(deps for deps in dependency_graph.values()),
            confidence=response.confidence,
            reasoning=response.strategy_reasoning,
        )

        # additional validation for circular dependencies
        if strategy == ExecutionStrategy.SEQUENTIAL and TaskDecomposer.has_circular_dependencies(dependency_graph):
            logger.warning("circular_dependencies_detected", falling_back_to_parallel=True)
            # fallback to parallel execution if circular dependencies detected
            strategy = ExecutionStrategy.PARALLEL
            dependency_graph = {obj: [] for obj in objectives}

        return strategy, dependency_graph

    @staticmethod
    def has_circular_dependencies(dependency_graph: dict[str, list[str]]) -> bool:
        """
        Detect circular dependencies using depth-first search
        """
        def has_cycle(node: str, visited: set[str], rec_stack: set[str]) -> bool:
            if node in rec_stack:
                return True
            if node in visited:
                return False

            visited.add(node)
            rec_stack.add(node)

            for dep in dependency_graph.get(node, []):
                if has_cycle(dep, visited, rec_stack):
                    return True

            rec_stack.remove(node)
            return False

        visited = set()
        for node in dependency_graph:
            if node not in visited:
                if has_cycle(node, visited, set()):
                    return True
        return False

    def _validate_dependency_graph(
        self,
        objectives: list[str],
        dependency_graph: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        """
        Validate and clean dependency graph
        """
        valid_objectives = set(objectives)
        cleaned_graph = {}

        for obj in objectives:
            if obj in dependency_graph:
                # filter out invalid dependencies
                valid_deps = [
                    dep for dep in dependency_graph[obj]
                    if dep in valid_objectives and dep != obj  # prevent self-dependency
                ]
                cleaned_graph[obj] = valid_deps
            else:
                cleaned_graph[obj] = []

        return cleaned_graph

    @staticmethod
    def generate_execution_order(dependency_graph: dict[str, list[str]]) -> list[str]:
        """
        Generate topological sort for execution order
        """
        from collections import deque, defaultdict

        # calculate in-degrees (how many dependencies each task has)
        in_degree = defaultdict(int)
        for node in dependency_graph:
            in_degree[node] = len(dependency_graph[node])

        # start with tasks that have no dependencies
        queue = deque([node for node, degree in in_degree.items() if degree == 0])
        result = []
        processed = set()

        while queue:
            node = queue.popleft()
            result.append(node)
            processed.add(node)

            # find tasks that were waiting for this task to complete
            for other_node, deps in dependency_graph.items():
                if node in deps and other_node not in processed:
                    in_degree[other_node] -= 1
                    if in_degree[other_node] == 0:
                        queue.append(other_node)

        return result

    @staticmethod
    async def decompose(
        llm: Any,
        objective: str,
        data: dict | list | None,
        analysis: TaskComplexityAnalysis | None = None
    ) -> tuple[ExecutionStrategy, list[TaskRequest]]:
        """
        Use LLM to decompose a complex task into subtasks with proper dependency analysis
        """

        # step 1: decompose into subtasks
        decomposition_prompt = f"""
You are extremely correct and diligent expert task planner.
Decompose the complex task into well-defined subtasks.
Don't go overboard though - keep subtasks focused and manageable.

Task: {objective}
Data: ```{json.dumps(data, indent=2) if data else "None"}```
Analysis: ```{analysis.model_dump_json() if analysis else "None"}```

Rules that you MUST folow no matter what:
- Keep subtasks focused and atomic
- Each subtask should have a clear, measurable objective
- ALL relevant data MUST BE included in the subtask data field
- DO NOT assume subtask have access to data outside of what you provide

IMPORTANT: Your response is parsed with `llm.with_structured_output()` so you MUST respond ONLY with a structured JSON response that is compatible with Pydantic.
    """

        logger.debug("decomposing_prompt", prompt=decomposition_prompt)

        response = await (
            llm
            .with_structured_output(SubtaskDecomposition)
            .ainvoke([HumanMessage(content=decomposition_prompt)])
        )

        logger.debug("decomposition_response", response=response)

        # step 2: extract objectives for dependency analysis
        subtasks = response.subtasks
        objectives = [subtask.objective for subtask in subtasks]

        # step 3: use analyze_dependencies to determine strategy and dependencies
        strategy, dependency_graph = await TaskDecomposer.analyze_dependencies(
            llm=llm,
            objectives=objectives,
            context=f"Primary task: {objective}"
        )

        logger.debug(
            "dependency_analysis_complete",
            strategy=strategy,
            dependencies=dependency_graph,
            subtask_count=len(subtasks)
        )

        # step 4: create task requests with proper ordering based on dependencies
        tasks = []

        if strategy == ExecutionStrategy.SEQUENTIAL:
            # order tasks based on dependency graph
            execution_order = TaskDecomposer.generate_execution_order(dependency_graph)
            ordered_subtasks = []

            # reorder subtasks based on dependency analysis
            subtask_map = {st.objective: st for st in subtasks}
            for objective in execution_order:
                if objective in subtask_map:
                    ordered_subtasks.append(subtask_map[objective])

            # add any missing subtasks (shouldn't happen, but safety check)
            for subtask in subtasks:
                if subtask not in ordered_subtasks:
                    ordered_subtasks.append(subtask)

            subtasks = ordered_subtasks

        # step 5: create TaskRequest objects
        for i, subtask in enumerate(subtasks):
            # determine priority based on dependencies and complexity
            priority = TaskPriority.HIGH if subtask.estimated_complexity == "high" else TaskPriority.MEDIUM

            # for sequential execution, higher priority for earlier tasks
            if strategy == ExecutionStrategy.SEQUENTIAL and i < len(subtasks) // 2:
                priority = TaskPriority.HIGH

            # for parallel execution, critical priority if many dependencies
            elif strategy == ExecutionStrategy.PARALLEL:
                deps_count = len(dependency_graph.get(subtask.objective, []))
                if deps_count > 0:
                    priority = TaskPriority.HIGH

            task_req = TaskRequest(
                task_type=subtask.type,
                objective=subtask.objective,
                data=subtask.data,
                priority=priority,
                constraints={
                    "dependencies": dependency_graph.get(subtask.objective, []),
                    "execution_strategy": strategy.value,
                    "position_in_sequence": i
                }
            )
            tasks.append(task_req)

        logger.info(
            "task_decomposition_complete",
            strategy=strategy,
            task_count=len(tasks),
            has_dependencies=any(dependency_graph.values())
        )

        return strategy, tasks
