import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentRunCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000, description="What you want done, in plain words")
    document_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=50, description="Documents the agent may use; empty = your latest ready ones"
    )


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]


class AgentStepOut(BaseModel):
    index: int
    kind: str = Field(description="route | tool_call | invalid_call | approval | rejected | final | error")
    tool_name: str | None
    thought: str | None
    arguments: dict[str, Any] | None
    summary: str | None
    ok: bool
    duration_ms: int | None


class ApprovalOut(BaseModel):
    id: uuid.UUID
    tool_name: str
    arguments: dict[str, Any]
    preview: str | None
    status: str
    expires_at: datetime
    decided_at: datetime | None


class CitationOut(BaseModel):
    number: int
    document_id: uuid.UUID
    document_title: str
    page: int | None
    snippet: str


class AgentRunOut(BaseModel):
    id: uuid.UUID
    goal: str
    document_ids: list[uuid.UUID]
    route: str | None
    route_reason: str | None
    route_confidence: float | None
    status: str = Field(description="running | waiting_approval | completed | failed")
    answer: str | None
    citations: list[CitationOut]
    confidence: str | None = Field(description="high | medium | low (specs/agent.md §10)")
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    error_code: str | None
    error_message: str | None
    pending_approval: ApprovalOut | None
    created_at: datetime | None
    updated_at: datetime | None
    steps: list[AgentStepOut] = Field(default_factory=list)


class AgentRunList(BaseModel):
    items: list[AgentRunOut]


class AgentOutputOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID | None
    kind: str
    title: str
    body: str
    due_date: date | None
    document_ids: list[uuid.UUID]
    created_at: datetime
