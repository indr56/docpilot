"""The tool registry: one list of every tool the agent (and later MCP) can call.

A tool = name + description written for the model + a Pydantic argument model + kind (read/write) +
an async handler. Handlers run as the calling user and only see documents in the run's scope.
"""

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Document, User
from app.services.usage import UsageEntry

ToolKind = Literal["read", "write", "handoff"]  # handoff: the runner pushes a specialist (specs §14)


class ToolError(Exception):
    """A tool could not do what was asked. The message goes back to the model, so it can adjust."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ToolResult:
    content: str  # what the model reads
    summary: str  # one line for the run's timeline
    max_chars: int | None = None  # overrides agent_observation_chars (get_document_text)


@dataclass
class ToolContext:
    db: AsyncSession
    user: User
    run: AgentRun
    scope: dict[uuid.UUID, Document]  # the documents this run may read, by id
    usages: list[UsageEntry] = field(default_factory=list)

    def document(self, document_id: str) -> Document:
        try:
            key = uuid.UUID(str(document_id))
        except ValueError as exc:
            raise ToolError("bad_document_id", f"'{document_id}' is not a document id from the list.") from exc
        document = self.scope.get(key)
        if document is None:
            raise ToolError("not_in_scope", f"Document {document_id} is not one of the documents in scope.")
        return document

    def add_source(self, document: Document, page: int | None, snippet: str) -> int:
        """Register a passage the model may cite; returns its number [n]. The same passage keeps its number."""
        snippet = " ".join(snippet.split())[:300]
        for i, source in enumerate(self.run.sources, start=1):
            if source["document_id"] == str(document.id) and source["page"] == page and source["snippet"] == snippet:
                return i
        self.run.sources = [
            *self.run.sources,
            {"document_id": str(document.id), "document_title": document.title, "page": page, "snippet": snippet},
        ]
        return len(self.run.sources)


Handler = Callable[[ToolContext, Any], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    kind: ToolKind
    handler: Handler
    # Write tools: a one-line description of what WILL happen, shown in the approval request.
    preview: Callable[[Any], str] | None = None

    def schema(self) -> dict[str, Any]:
        return _compact_schema(self.args_model.model_json_schema())

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "schema": self.schema(), "kind": self.kind}


def _compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop titles: they cost tokens and repeat the field names."""
    if isinstance(schema, dict):
        # A "title" whose value is a string is a label; a "title" holding a dict is a field named title.
        return {k: _compact_schema(v) for k, v in schema.items() if not (k == "title" and isinstance(v, str))}  # type: ignore[misc]
    if isinstance(schema, list):
        return [_compact_schema(v) for v in schema]  # type: ignore[return-value]
    return schema


class InvalidCall(Exception):
    """The model's tool call can't run as sent; the message tells it what to fix."""


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", tool.name):
        raise ValueError(f"bad tool name: {tool.name}")
    if tool.kind == "write" and tool.preview is None:
        raise ValueError(f"write tool {tool.name} needs a preview")
    _REGISTRY[tool.name] = tool
    return tool


def get_tool(name: str) -> Tool | None:
    _load()
    return _REGISTRY.get(name)


def all_tools() -> list[Tool]:
    _load()
    return list(_REGISTRY.values())


def _load() -> None:
    # Importing the modules registers their tools.
    from app.tools import handoff_tools, read_tools, write_tools  # noqa: F401


def parse_call(name: str, arguments_json: str, allowed: list[str]) -> tuple[Tool, BaseModel]:
    """Validate a tool call from the model: known tool, offered on this route, JSON object, schema."""
    tool = get_tool(name)
    if tool is None or name not in allowed:
        offered = ", ".join(allowed) or "none"
        raise InvalidCall(f"There is no tool named '{name}' here. Tools you can call: {offered}.")
    try:
        raw = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as exc:
        raise InvalidCall(f"arguments_json is not valid JSON ({exc.msg}). Send a JSON object.") from exc
    if not isinstance(raw, dict):
        raise InvalidCall('arguments_json must be a JSON object, like {"query": "..."}.')
    try:
        return tool, tool.args_model.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
        raise InvalidCall(f"The arguments for {name} don't match its schema: {problems}.") from exc
