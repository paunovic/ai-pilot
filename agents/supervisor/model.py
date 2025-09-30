from pydantic import BaseModel
from typing import Literal


class TaskDecompositionAnalysis(BaseModel):
    """Schema for task decomposition analysis"""

    strategy: Literal["sequential", "parallel", "consensus"]
    strategy_reasoning: str
    dependency_graph: dict[str, list[str]]
    confidence: float
    execution_order: list[str]
    parallel_groups: list[list[str]]
    risk_factors: list[str]
    optimization_notes: list[str]


class SubtaskDecomposition(BaseModel):
    """Schema for subtask decomposition"""

    class Subtask(BaseModel):
        objective: str
        type: Literal["research", "analysis", "synthesis", "validation", "generation"]
        estimated_complexity: Literal["low", "medium", "high"]
        data: dict | None = None

    subtasks: list[Subtask]
