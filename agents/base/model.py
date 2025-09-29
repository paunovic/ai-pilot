from dataclasses import dataclass, field
import arrow
from enum import Enum
import uuid

from pydantic import BaseModel, Field, field_validator, field_serializer
import structlog


logger = structlog.get_logger()


class TaskStatus(str, Enum):
    """Task execution status"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class TaskPriority(str, Enum):
    """Task priority levels"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ExecutionStrategy(str, Enum):
    """How to execute multiple tasks"""
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    CONSENSUS = "consensus"


class AgentCapability(str, Enum):
    """Agent specialized capabilities"""
    SUPERVISOR = "supervisor"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    SYNTHESIS = "synthesis"
    VALIDATION = "validation"
    GENERATION = "generation"


@dataclass
class TaskContext:
    """Minimal context passed to subagents"""
    background: str | None = None
    constraints: dict | list = field(default_factory=dict)
    output_format: str = "structured_json"
    timeout_ms: int = 30000
    max_tokens: int = 2000


class TaskRequest(BaseModel):
    """Structured task request from primary to subagent"""
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_type: str
    objective: str
    data: dict | list | None = None
    context: TaskContext | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    constraints: dict | list = Field(default_factory=dict)
    created_at: arrow.Arrow = Field(default_factory=lambda: arrow.utcnow())

    class Config:
        use_enum_values = True
        arbitrary_types_allowed = True

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_arrow(cls, v):
        if isinstance(v, arrow.Arrow):
            return v
        if isinstance(v, str):
            return arrow.get(v)
        raise ValueError("created_at must be an arrow.Arrow or ISO8601 string")

    @field_serializer("created_at")
    def serialize_arrow(self, v: arrow.Arrow, _info):
        return v.isoformat()

    def to_json(self) -> str:
        """Serialize to JSON for agent communication"""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, json_str: str) -> "TaskRequest":
        """Deserialize from JSON"""
        return cls.model_validate_json(json_str)


class TaskResponse(BaseModel):
    """Structured response from subagent to primary"""
    task_id: str
    status: TaskStatus
    result: dict | list | None = None
    error: str | None = None
    error_type: str | None = None
    partial_result: dict | list |None = None
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    confidence_reasoning: str | None = None
    processing_time_ms: int = 0
    tokens_used: int = 0
    cost: float = 0.0
    metadata: dict = Field(default_factory=dict)
    recommendations: list[str] = Field(default_factory=list)
    completed_at: arrow.Arrow = Field(default_factory=lambda: arrow.utcnow())

    class Config:
        use_enum_values = True
        arbitrary_types_allowed = True

    @field_validator("completed_at", mode="before")
    @classmethod
    def parse_arrow(cls, v):
        if isinstance(v, arrow.Arrow):
            return v
        if isinstance(v, str):
            return arrow.get(v)
        raise ValueError("completed_at must be an arrow.Arrow or ISO8601 string")

    @field_serializer("completed_at")
    def serialize_arrow(self, v: arrow.Arrow, _info):
        return v.isoformat()

    def to_json(self) -> str:
        """Serialize to JSON for agent communication"""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, json_str: str) -> "TaskResponse":
        """Deserialize from JSON"""
        return cls.model_validate_json(json_str)


@dataclass
class ExecutionTrace:
    """Track agent execution for monitoring"""
    agent_name: str
    task_id: str
    start_time: arrow.Arrow = field(default_factory=arrow.utcnow)
    end_time: arrow.Arrow | None = None
    status: TaskStatus = TaskStatus.PENDING
    tokens_used: int = 0
    cost: float = 0.0
    error: str | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.end_time:
            return int((self.end_time - self.start_time).total_seconds() * 1000)
        return None
