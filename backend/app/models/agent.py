"""Agent runs, their steps, approvals for write actions, and what write tools save (specs/agent.md §8)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.models.enums import AgentRunStatus, ApprovalStatus


class AgentRun(UUIDPrimaryKey, Timestamps, Base):
    """One goal handed to the agent. `messages` is the model's transcript, kept so a run can resume
    after an approval; `sources` maps citation numbers [n] to the passages tools returned."""

    __tablename__ = "agent_runs"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    document_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    route: Mapped[str | None] = mapped_column(String(30))
    route_reason: Mapped[str | None] = mapped_column(Text)
    route_confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default=AgentRunStatus.RUNNING, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(10))
    messages: Mapped[list[dict[str, str]]] = mapped_column(JSONB, default=list, nullable=False)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalid_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)

    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentStep.index", lazy="selectin"
    )
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Approval.created_at", lazy="selectin"
    )

    __table_args__ = (Index("ix_agent_runs_owner_created", "owner_id", "created_at"),)


class AgentStep(UUIDPrimaryKey, Timestamps, Base):
    """What happened at one point of a run: the route, a tool call, an invalid call, an approval, the end."""

    __tablename__ = "agent_steps"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(50))
    thought: Mapped[str | None] = mapped_column(Text)
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)  # short, for the timeline; the full result is in messages
    ok: Mapped[bool] = mapped_column(default=True, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    run: Mapped[AgentRun] = relationship(back_populates="steps")

    __table_args__ = (UniqueConstraint("run_id", "index", name="uq_agent_steps_run_index"),)


class Approval(UUIDPrimaryKey, Timestamps, Base):
    """A write tool call waiting for the user. The stored arguments are what runs on approve."""

    __tablename__ = "approvals"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=ApprovalStatus.PENDING, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[AgentRun] = relationship(back_populates="approvals")

    __table_args__ = (Index("ix_approvals_run", "run_id"),)


class AgentOutput(UUIDPrimaryKey, Timestamps, Base):
    """What write tools save: a report (Markdown) or a task."""

    __tablename__ = "agent_outputs"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # "report" | "task"
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    document_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    __table_args__ = (Index("ix_agent_outputs_owner_created", "owner_id", "created_at"),)
