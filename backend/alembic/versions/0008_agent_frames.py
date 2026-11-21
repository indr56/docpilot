"""agent runs keep a stack of agent frames (supervisor + specialists); steps record their agent

Revision ID: 0008_agent_frames
Revises: 0007_agent_tables
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_agent_frames"
down_revision: str | None = "0007_agent_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A single-agent run's transcript becomes a one-frame stack.
    op.add_column(
        "agent_runs",
        sa.Column("stack", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
    )
    op.execute(
        "UPDATE agent_runs SET stack = jsonb_build_array(jsonb_build_object("
        "'agent', 'research', 'route', coalesce(route, 'unsure'), 'tools', '[]'::jsonb, 'messages', messages))"
        " WHERE jsonb_array_length(messages) > 0"
    )
    op.drop_column("agent_runs", "messages")
    op.add_column("agent_steps", sa.Column("agent", sa.String(length=20), server_default="research", nullable=False))


def downgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("messages", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
    )
    op.execute("UPDATE agent_runs SET messages = coalesce(stack -> 0 -> 'messages', '[]'::jsonb)")
    op.drop_column("agent_runs", "stack")
    op.drop_column("agent_steps", "agent")
