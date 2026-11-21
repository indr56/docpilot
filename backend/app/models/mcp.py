"""MCP: server-side confirmations for writes, and the call log (specs/mcp.md §5-§6)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class McpConfirmation(UUIDPrimaryKey, Timestamps, Base):
    """A write an MCP client asked for and must confirm: single-use, bound to the key, the tool and
    the arguments' hash, and short-lived. `result` is kept so a repeat returns it instead of writing again."""

    __tablename__ = "mcp_confirmations"

    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending | executed
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (Index("ix_mcp_confirmations_key", "api_key_id"),)


class McpCall(UUIDPrimaryKey, Timestamps, Base):
    """One MCP tool call. Arguments are never stored, only their hash and size."""

    __tablename__ = "mcp_calls"

    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_mcp_calls_owner_created", "owner_id", "created_at"),)
