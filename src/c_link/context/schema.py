from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class ItemType(str, Enum):
    FACT = "FACT"
    DECISION = "DECISION"
    GOAL = "GOAL"
    CONSTRAINT = "CONSTRAINT"
    ASSUMPTION = "ASSUMPTION"
    QUESTION = "QUESTION"
    TASK = "TASK"
    RESULT = "RESULT"
    PREFERENCE = "PREFERENCE"
    EVIDENCE = "EVIDENCE"
    REJECTED_OPTION = "REJECTED_OPTION"


class ItemStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    RESOLVED = "RESOLVED"
    OPEN = "OPEN"
    BLOCKED = "BLOCKED"
    ARCHIVED = "ARCHIVED"


class ContextItem(BaseModel):
    id: str
    type: ItemType
    status: ItemStatus = ItemStatus.ACTIVE
    content: str
    reason: str | None = None
    source: str = "system"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActiveContext(BaseModel):
    schema_version: int = 1
    session_id: str
    project_id: str | None = None
    current_objective: str | None = None
    current_state: str | None = None
    items: list[ContextItem] = Field(default_factory=list)
    completed_work: list[str] = Field(default_factory=list)
    open_tasks: list[str] = Field(default_factory=list)
    recent_results: list[str] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    next_step: str | None = None
    context_version: int = 0
