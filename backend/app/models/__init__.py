"""Import all models so SQLAlchemy metadata (and Alembic) sees every table."""

from app.models.agent import AgentOutput, AgentRun, AgentStep, Approval
from app.models.chat import ChatMessage, ChatSession, chat_session_documents
from app.models.document import Document, DocumentChunk, DocumentInsight, DocumentSummary
from app.models.metrics import AIUsage, Comparison, ProcessingRun, RequestMetric, StageRun
from app.models.user import ApiKey, User

__all__ = [
    "AIUsage",
    "AgentOutput",
    "AgentRun",
    "AgentStep",
    "ApiKey",
    "Approval",
    "ChatMessage",
    "ChatSession",
    "Comparison",
    "Document",
    "DocumentChunk",
    "DocumentInsight",
    "DocumentSummary",
    "ProcessingRun",
    "RequestMetric",
    "StageRun",
    "User",
    "chat_session_documents",
]
