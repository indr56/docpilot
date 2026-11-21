"""Everything DocPilot-specific about MCP, with no dependency on the MCP SDK (specs/mcp.md).

Keys and scopes, the tool list and schemas, in-process dispatch to the same tool registry the agent
uses, lean results, server-side confirmation for writes, the per-key rate limit and the call log.
`app/mcp/server.py` adapts this to the SDK.
"""

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import runner
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.rate_limit import check_rate_limit
from app.core.security import hash_api_key
from app.db.base import utcnow
from app.db.session import get_async_sessionmaker
from app.models import AgentRun, ApiKey, Document, DocumentInsight, McpCall, McpConfirmation, User
from app.tools.registry import InvalidCall, Tool, ToolContext, ToolError, get_tool, parse_call

logger = get_logger(__name__)

READ_SCOPE, WRITE_SCOPE = "mcp:read", "mcp:write"
READ_TOOLS = [
    "search_passages",
    "get_document_text",
    "get_document_notes",
    "get_insights",
    "scan_document",
    "compare_documents",
]
WRITE_TOOLS = ["save_report", "create_task"]
SCOPE_DOCUMENTS = 200  # the documents a read tool may open: the key owner's latest ones


class McpError(Exception):
    """A tool call that can't succeed. Sent to the client as an error whose text starts with the code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class Principal:
    key_id: uuid.UUID
    user_id: uuid.UUID
    scopes: frozenset[str]


async def principal_for_key(raw_key: str) -> Principal | None:
    """The active key behind a bearer token, or None (the SDK then answers 401)."""
    async with get_async_sessionmaker()() as db:
        key = (
            await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key), ApiKey.is_active.is_(True)))
        ).scalar_one_or_none()
        if key is None:
            return None
        return Principal(key.id, key.user_id, frozenset(key.scopes or []))


# --------------------------------------------------------------------------- the tool list
class ListArgs(BaseModel):
    limit: int = Field(default=20, ge=1, le=100, description="How many documents per page")
    cursor: str | None = Field(default=None, description="next_cursor from the previous page")
    include: list[Literal["summary", "tags"]] = Field(default_factory=list, description="Extra fields per document")


class AskArgs(BaseModel):
    question: str = Field(min_length=2, max_length=2000, description="The question, in plain words")
    document_ids: list[str] = Field(
        default_factory=list, max_length=50, description="Documents to use; empty = your latest ready ones"
    )
    include: list[Literal["snippets", "steps"]] = Field(default_factory=list, description="Extra detail to return")


_INCLUDE_SNIPPETS = {
    "type": "array",
    "items": {"enum": ["snippets"]},
    "description": "Add each citation's text snippet (left out by default to keep the result small)",
}
_CONFIRMATION = {
    "type": ["string", "null"],
    "description": "Leave empty on the first call. Send the confirmation_id that call returned, with the same "
    "arguments, to save.",
}
_CONFIRM_NOTE = (
    " Two steps: the first call saves nothing and returns a confirmation_id and a preview to show the user; "
    "call again with the same arguments and that confirmation_id to save."
)


def _compact(schema: dict[str, Any]) -> dict[str, Any]:
    from app.tools.registry import _compact_schema

    return _compact_schema(schema)


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    scope: str


def tools() -> list[McpTool]:
    out = [
        McpTool(
            "list_documents",
            "List your documents, newest first: id, title, status and pages; add summary or tags with include. "
            "Returns next_cursor when there are more.",
            _compact(ListArgs.model_json_schema()),
            READ_SCOPE,
        ),
        McpTool(
            "ask_documents",
            "Answer a question from your documents in one call (search, read and answer), with numbered "
            "citations to document and page. Read-only.",
            _compact(AskArgs.model_json_schema()),
            READ_SCOPE,
        ),
    ]
    for name in READ_TOOLS:
        tool = _registry_tool(name)
        schema = tool.schema()
        schema["properties"] = {**schema.get("properties", {}), "include": _INCLUDE_SNIPPETS}
        out.append(McpTool(name, tool.description, schema, READ_SCOPE))
    for name in WRITE_TOOLS:
        tool = _registry_tool(name)
        schema = tool.schema()
        schema["properties"] = {**schema.get("properties", {}), "confirmation_id": _CONFIRMATION}
        description = tool.description.replace(" (asks the user to approve first).", ".") + _CONFIRM_NOTE
        out.append(McpTool(name, description, schema, WRITE_SCOPE))
    return out


def _registry_tool(name: str) -> Tool:
    tool = get_tool(name)
    assert tool is not None, name
    return tool


def lint_descriptions(items: list[McpTool]) -> list[str]:
    """Problems with tool descriptions, as the model reads them (specs/mcp.md §6). Empty = fine."""
    problems = []
    for t in items:
        d = t.description
        if not 40 <= len(d) <= 400:
            problems.append(f"{t.name}: {len(d)} characters (want 40-400)")
        if not d[:1].isupper():
            problems.append(f"{t.name}: should start with a capital letter")
        if "TODO" in d or "FIXME" in d:
            problems.append(f"{t.name}: has a TODO")
        if not any(w in d.lower() for w in ("return", "list", "answer", "save", "create", "the whole", "compar")):
            problems.append(f"{t.name}: doesn't say what it returns or does")
        if t.scope == WRITE_SCOPE and "confirmation_id" not in d:
            problems.append(f"{t.name}: a write tool must explain the confirmation step")
        for prop, spec in t.input_schema.get("properties", {}).items():
            if not spec.get("description"):
                problems.append(f"{t.name}.{prop}: no description")
    return problems


# --------------------------------------------------------------------------- dispatch
def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


async def _context(db: AsyncSession, user: User) -> ToolContext:
    documents = (
        (
            await db.execute(
                select(Document)
                .where(Document.owner_id == user.id, Document.is_latest.is_(True))
                .order_by(Document.created_at.desc())
                .limit(SCOPE_DOCUMENTS)
            )
        )
        .scalars()
        .all()
    )
    # A transient run: tools register citations on it; it is never saved (write outputs get run_id NULL).
    run = AgentRun(owner_id=user.id, goal="mcp", document_ids=[], sources=[])
    return ToolContext(db=db, user=user, run=run, scope={d.id: d for d in documents})


def _citations(ctx: ToolContext, include: list[str]) -> list[dict[str, Any]]:
    out = []
    for n, s in enumerate(ctx.run.sources, start=1):
        item = {"n": n, "document_id": s["document_id"], "title": s["document_title"], "page": s["page"]}
        if "snippets" in include:
            item["snippet"] = s["snippet"]
        out.append(item)
    return out


def _parse(name: str, arguments: dict[str, Any]) -> tuple[Tool, BaseModel]:
    try:
        return parse_call(name, json.dumps(arguments), [name])
    except InvalidCall as exc:
        raise McpError("invalid_arguments", str(exc)) from exc


async def _run(ctx: ToolContext, tool: Tool, args: BaseModel) -> str:
    try:
        return (await tool.handler(ctx, args)).content
    except ToolError as exc:
        raise McpError(exc.code, exc.message) from exc
    except AppError as exc:
        raise McpError(exc.code, exc.message) from exc


async def _list_documents(db: AsyncSession, user: User, args: ListArgs) -> dict[str, Any]:
    try:
        offset = int(args.cursor or 0)
    except ValueError as exc:
        raise McpError("invalid_cursor", "Use the next_cursor value from the previous page.") from exc
    stmt = (
        select(Document)
        .where(Document.owner_id == user.id, Document.is_latest.is_(True))
        .order_by(Document.created_at.desc(), Document.id)
        .offset(offset)
        .limit(args.limit + 1)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    page, more = rows[: args.limit], len(rows) > args.limit
    insights: dict[uuid.UUID, DocumentInsight] = {}
    if args.include and page:
        found = await db.execute(select(DocumentInsight).where(DocumentInsight.document_id.in_([d.id for d in page])))
        insights = {i.document_id: i for i in found.scalars()}
    items = []
    for d in page:
        item: dict[str, Any] = {"id": str(d.id), "title": d.title, "status": d.status, "pages": d.page_count}
        insight = insights.get(d.id)
        if "summary" in args.include:
            item["summary"] = insight.summary if insight else None
        if "tags" in args.include:
            item["tags"] = insight.tags if insight else []
        items.append(item)
    return {"items": items, "next_cursor": str(offset + args.limit) if more else None}


async def _ask(db: AsyncSession, user: User, args: AskArgs) -> dict[str, Any]:
    try:
        ids = [uuid.UUID(i) for i in args.document_ids]
    except ValueError as exc:
        raise McpError("bad_document_id", "document_ids must be document ids from list_documents.") from exc
    done: dict[str, Any] = {}
    try:
        async for event, data in runner.start_run(db, user, args.question, ids, read_only=True):
            if event == "done":
                done = data
    except AppError as exc:
        raise McpError(exc.code, exc.message) from exc
    if done.get("status") == "failed":
        raise McpError(done.get("error_code") or "run_failed", done.get("error_message") or "The run failed.")
    citations = [
        {"n": c["number"], "document_id": c["document_id"], "title": c["document_title"], "page": c["page"]}
        | ({"snippet": c["snippet"]} if "snippets" in args.include else {})
        for c in done.get("citations", [])
    ]
    out: dict[str, Any] = {
        "result": done.get("answer"),
        "citations": citations,
        "confidence": done.get("confidence"),
        "run_id": done.get("id"),
    }
    if "steps" in args.include:
        out["steps"] = [{"agent": s["agent"], "kind": s["kind"], "summary": s["summary"]} for s in done["steps"]]
    return out


async def _write(
    db: AsyncSession, user: User, principal: Principal, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    confirmation_id = arguments.pop("confirmation_id", None)
    tool, args = _parse(name, arguments)
    args_hash = _hash({"tool": name, "arguments": args.model_dump(mode="json")})
    if not confirmation_id:
        ttl = timedelta(minutes=get_settings().mcp_confirmation_ttl_minutes)
        pending = McpConfirmation(
            api_key_id=principal.key_id, tool_name=name, arguments_hash=args_hash, expires_at=utcnow() + ttl
        )
        db.add(pending)
        await db.commit()
        assert tool.preview is not None
        return {
            "status": "confirmation_required",
            "confirmation_id": str(pending.id),
            "preview": tool.preview(args),
            "expires_at": pending.expires_at.isoformat(),
        }

    try:
        cid = uuid.UUID(str(confirmation_id))
    except ValueError as exc:
        raise McpError("confirmation_not_found", "That confirmation_id doesn't exist.") from exc
    confirmation = await db.get(McpConfirmation, cid)
    if confirmation is None or confirmation.api_key_id != principal.key_id:
        raise McpError("confirmation_not_found", "That confirmation_id doesn't exist for this key.")
    if confirmation.status == "executed":
        return {"status": "already_executed", "result": (confirmation.result or {}).get("result")}
    if confirmation.tool_name != name or confirmation.arguments_hash != args_hash:
        raise McpError(
            "confirmation_mismatch", "The arguments differ from the ones confirmed. Start again without the id."
        )
    # Claim it: only one caller can move it out of pending, and only before it expires.
    claimed = (
        await db.execute(
            update(McpConfirmation)
            .where(
                McpConfirmation.id == cid,
                McpConfirmation.status == "pending",
                McpConfirmation.expires_at > utcnow(),
            )
            .values(status="executing")
            .returning(McpConfirmation.id)
        )
    ).first()
    if claimed is None:
        await db.rollback()
        await db.refresh(confirmation)
        if confirmation.status in ("executed", "executing"):
            return {"status": "already_executed", "result": (confirmation.result or {}).get("result")}
        raise McpError("confirmation_expired", "This confirmation expired. Start again without the id.")
    await db.commit()

    ctx = await _context(db, user)
    try:
        result = await _run(ctx, tool, args)
    except McpError:
        # Nothing was saved: release the claim so the same confirmation can be retried.
        await db.rollback()
        await db.execute(update(McpConfirmation).where(McpConfirmation.id == cid).values(status="pending"))
        await db.commit()
        raise
    await db.execute(
        update(McpConfirmation)
        .where(McpConfirmation.id == cid)
        .values(status="executed", executed_at=utcnow(), result={"result": result})
    )
    await db.commit()  # the write and "executed" land together
    return {"status": "executed", "result": result}


async def call_tool(principal: Principal, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one MCP tool call for a key. Raises McpError on any failure; every call is logged."""
    started = time.monotonic()
    error_code: str | None = None
    arguments = dict(arguments or {})
    raw_hash, raw_bytes = _hash(arguments), len(json.dumps(arguments, default=str))
    try:
        spec = next((t for t in tools() if t.name == name), None)
        if spec is None:
            raise McpError("unknown_tool", f"There is no tool named '{name}'.")
        if spec.scope not in principal.scopes:
            raise McpError("forbidden", f"This key doesn't have the {spec.scope} scope needed for {name}.")
        try:
            await check_rate_limit(str(principal.key_id), "mcp", get_settings().mcp_rate_limit_per_minute)
        except AppError as exc:
            raise McpError("rate_limited", exc.message) from exc
        async with get_async_sessionmaker()() as db:
            user = await db.get(User, principal.user_id)
            if user is None:
                raise McpError("unauthorized", "The key's user no longer exists.")
            if name == "list_documents":
                return await _list_documents(db, user, _validate(ListArgs, arguments))
            if name == "ask_documents":
                return await _ask(db, user, _validate(AskArgs, arguments))
            if name in WRITE_TOOLS:
                return await _write(db, user, principal, name, arguments)
            include = arguments.pop("include", None) or []
            tool, args = _parse(name, arguments)
            ctx = await _context(db, user)
            result = await _run(ctx, tool, args)
            return {"result": result, "citations": _citations(ctx, include)}
    except McpError as exc:
        error_code = exc.code
        raise
    except Exception:
        error_code = "internal_error"
        logger.exception("mcp_tool_failed", tool=name)
        raise McpError("internal_error", "The tool failed unexpectedly.") from None
    finally:
        await _log(principal, name, error_code, started, raw_hash, raw_bytes)


def _validate[M: BaseModel](model: type[M], arguments: dict[str, Any]) -> M:
    from pydantic import ValidationError

    try:
        return model.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
        raise McpError("invalid_arguments", problems) from exc


async def _log(
    principal: Principal, name: str, error_code: str | None, started: float, args_hash: str, args_bytes: int
) -> None:
    try:
        async with get_async_sessionmaker()() as db:
            db.add(
                McpCall(
                    api_key_id=principal.key_id,
                    owner_id=principal.user_id,
                    tool_name=name[:50],
                    ok=error_code is None,
                    error_code=error_code,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    arguments_hash=args_hash,
                    arguments_bytes=args_bytes,
                )
            )
            await db.commit()
    except Exception:  # pragma: no cover - logging must never fail the call
        logger.exception("mcp_call_log_failed", tool=name)
