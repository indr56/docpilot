"""The agent loop, approvals and the run's lifecycle (specs/agent.md §5-§7).

Every entry point is an async generator of (event, data) pairs, so the streaming endpoint sends them
as Server-Sent Events and the plain endpoint just runs them to the end. The run is saved after every
step (a checkpoint), so GET /agent/runs/{id} shows progress and a paused run can resume later.
"""

import hashlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import route_goal
from app.agent.routes import DOCUMENT_ROUTES, ROUTE_TOOLS
from app.core.config import get_settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.llm import get_provider
from app.llm.prompts import agent as agent_prompt
from app.llm.schemas import AgentDecision
from app.models import AgentOutput, AgentRun, AgentStep, Approval, Document, User
from app.models.enums import AgentRoute, AgentRunStatus, ApprovalStatus, DocumentStatus
from app.services import documents as document_service
from app.services.jobs import redis_lock
from app.services.usage import UsageEntry, add_entries, estimate_cost_usd, failed_calls_to
from app.tools.registry import InvalidCall, Tool, ToolContext, ToolError, ToolResult, get_tool, parse_call

logger = get_logger(__name__)

Event = tuple[str, dict[str, Any]]
CITATION = re.compile(r"\[(\d{1,3})\]")


class RunFailed(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- shapes sent to clients
def step_out(step: AgentStep) -> dict[str, Any]:
    return {
        "index": step.index,
        "kind": step.kind,
        "tool_name": step.tool_name,
        "thought": step.thought,
        "arguments": step.arguments,
        "summary": step.summary,
        "ok": step.ok,
        "duration_ms": step.duration_ms,
    }


def approval_out(approval: Approval, preview: str | None = None) -> dict[str, Any]:
    tool = get_tool(approval.tool_name)
    if preview is None and tool is not None and tool.preview is not None:
        try:
            preview = tool.preview(tool.args_model.model_validate(approval.arguments))
        except Exception:  # pragma: no cover - stored arguments were validated before saving
            preview = approval.tool_name
    return {
        "id": str(approval.id),
        "tool_name": approval.tool_name,
        "arguments": approval.arguments,
        "preview": preview,
        "status": approval.status,
        "expires_at": approval.expires_at.isoformat(),
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
    }


def run_out(run: AgentRun, *, with_steps: bool = True) -> dict[str, Any]:
    pending = next((a for a in run.approvals if a.status == ApprovalStatus.PENDING), None)
    out: dict[str, Any] = {
        "id": str(run.id),
        "goal": run.goal,
        "document_ids": run.document_ids,
        "route": run.route,
        "route_reason": run.route_reason,
        "route_confidence": run.route_confidence,
        "status": run.status,
        "answer": run.answer,
        "citations": run.citations,
        "confidence": run.confidence,
        "tool_calls": run.tool_calls,
        "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens,
        "cost_usd": float(run.cost_usd),
        "error_code": run.error_code,
        "error_message": run.error_message,
        "pending_approval": approval_out(pending) if pending else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }
    if with_steps:
        out["steps"] = [step_out(s) for s in run.steps]
    return out


# --------------------------------------------------------------------------- helpers
def _add_step(run: AgentRun, kind: str, **fields: Any) -> AgentStep:
    step = AgentStep(index=len(run.steps), kind=kind, **fields)
    run.steps.append(step)
    return step


def _absorb(run: AgentRun, entries: list[UsageEntry]) -> None:
    for entry in entries:
        if entry.cached:
            continue
        run.prompt_tokens += entry.usage.prompt_tokens
        run.completion_tokens += entry.usage.completion_tokens
        run.cost_usd = Decimal(run.cost_usd) + estimate_cost_usd(entry.usage)


async def _checkpoint(db: AsyncSession, run: AgentRun, usages: list[UsageEntry]) -> None:
    """Save the run, its new steps and the usage rows of the calls made since the last checkpoint."""
    _absorb(run, usages)
    add_entries(db, usages, owner_id=run.owner_id)
    usages.clear()
    run.updated_at = utcnow()
    await db.commit()


def _append(run: AgentRun, *messages: dict[str, str]) -> None:
    run.messages = [*run.messages, *messages]  # reassign so SQLAlchemy sees the JSONB change


def _observation(result: ToolResult) -> str:
    limit = result.max_chars or get_settings().agent_observation_chars
    if len(result.content) <= limit:
        return result.content
    return result.content[:limit] + f"\n[truncated: {len(result.content) - limit} more characters]"


def _arguments_hash(tool_name: str, arguments: dict[str, Any]) -> str:
    return hashlib.sha256(f"{tool_name}:{json.dumps(arguments, sort_keys=True)}".encode()).hexdigest()


def _citations(answer: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: list[int] = []
    for match in CITATION.finditer(answer):
        n = int(match.group(1))
        if 1 <= n <= len(sources) and n not in seen:
            seen.append(n)
    return [{"number": n, **sources[n - 1]} for n in seen]


_NOT_COVERED = re.compile(
    r"\b(don't|do not|doesn't|does not|couldn't|could not)\b.{0,40}\b(contain|mention|say|cover|find)", re.I
)


def _confidence(run: AgentRun) -> str:
    if run.route in DOCUMENT_ROUTES and (not run.citations or _NOT_COVERED.search(run.answer or "")):
        return "low"
    if run.route == AgentRoute.UNSURE or any(s.kind == "invalid_call" for s in run.steps):
        return "medium"
    return "high"


async def _scope_documents(db: AsyncSession, user: User, document_ids: list[str]) -> dict[uuid.UUID, Document]:
    ids = [uuid.UUID(i) for i in document_ids]
    rows = (
        (await db.execute(select(Document).where(Document.id.in_(ids), Document.owner_id == user.id))).scalars().all()
    )
    by_id = {d.id: d for d in rows}
    return {i: by_id[i] for i in ids if i in by_id}  # a document deleted mid-run simply drops out


async def _decide(messages: list[dict[str, str]], usages: list[UsageEntry]) -> AgentDecision:
    provider = get_provider()
    with failed_calls_to(usages.append, "agent_step", provider.chat_model, agent_prompt.VERSION):
        result = await provider.acomplete_structured(messages, AgentDecision, max_tokens=2000)
    usages.append(UsageEntry("agent_step", result.usage, agent_prompt.VERSION))
    return result.value


async def _execute(ctx: ToolContext, tool: Tool, args: BaseModel) -> tuple[ToolResult, bool]:
    """Run a tool; a failure becomes an observation the model can react to, never a crashed run."""
    try:
        return await tool.handler(ctx, args), True
    except ToolError as exc:
        return ToolResult(f"Error ({exc.code}): {exc.message}", f"error: {exc.code}"), False
    except AppError as exc:
        return ToolResult(f"Error ({exc.code}): {exc.message}", f"error: {exc.code}"), False
    except Exception:
        logger.exception("agent_tool_failed", tool=tool.name, run_id=str(ctx.run.id))
        return ToolResult("Error (tool_failed): the tool failed unexpectedly.", "error: tool_failed"), False


async def _fail(db: AsyncSession, run: AgentRun, usages: list[UsageEntry], code: str, message: str) -> Event:
    run.status = AgentRunStatus.FAILED
    run.error_code = code
    run.error_message = message
    _add_step(run, "error", summary=f"{code}: {message}", ok=False)
    await _checkpoint(db, run, usages)
    logger.info("agent_run_failed", run_id=str(run.id), code=code)
    return ("done", run_out(run))


# --------------------------------------------------------------------------- the loop
async def _loop(db: AsyncSession, user: User, run: AgentRun, ctx: ToolContext) -> AsyncIterator[Event]:
    settings = get_settings()
    started = time.monotonic()
    allowed = ROUTE_TOOLS[run.route or AgentRoute.UNSURE]
    usages = ctx.usages
    while True:
        if time.monotonic() - started > settings.agent_timeout_seconds:
            yield await _fail(db, run, usages, "timeout", f"The run took over {settings.agent_timeout_seconds} s.")
            return
        _absorb(run, usages)  # count calls made since the last checkpoint before checking the ceiling
        add_entries(db, usages, owner_id=run.owner_id)
        usages.clear()
        if float(run.cost_usd) >= settings.agent_max_cost_usd:
            yield await _fail(
                db, run, usages, "cost_limit", f"The run reached its ${settings.agent_max_cost_usd} limit."
            )
            return

        decision = await _decide(run.messages, usages)
        _append(run, {"role": "assistant", "content": decision.model_dump_json()})

        if decision.action == "final":
            if not decision.answer.strip():
                _append(run, agent_prompt.tool_result("final", "Error: the final answer is empty. Write the answer."))
                _add_step(run, "invalid_call", thought=decision.thought, summary="empty final answer", ok=False)
                run.invalid_calls += 1
                if run.invalid_calls >= settings.agent_max_invalid_calls:
                    yield await _fail(db, run, usages, "invalid_tool_calls", "The model kept sending invalid calls.")
                    return
                continue
            run.answer = decision.answer.strip()
            run.citations = _citations(run.answer, run.sources)
            run.status = AgentRunStatus.COMPLETED
            run.confidence = _confidence(run)
            step = _add_step(
                run, "final", thought=decision.thought, summary=f"answered, {len(run.citations)} citations"
            )
            await _checkpoint(db, run, usages)
            yield ("step", step_out(step))
            yield ("done", run_out(run))
            return

        if run.tool_calls >= settings.agent_max_steps:
            yield await _fail(db, run, usages, "step_limit", f"The run used all {settings.agent_max_steps} tool calls.")
            return

        try:
            tool, args = parse_call(decision.tool_name, decision.arguments_json, allowed)
        except InvalidCall as exc:
            run.invalid_calls += 1
            _append(run, agent_prompt.tool_result(decision.tool_name or "unknown", f"Error (invalid_call): {exc}"))
            step = _add_step(
                run,
                "invalid_call",
                tool_name=decision.tool_name[:50] or None,
                thought=decision.thought,
                summary=str(exc)[:500],
                ok=False,
            )
            if run.invalid_calls >= settings.agent_max_invalid_calls:
                yield await _fail(db, run, usages, "invalid_tool_calls", "Three invalid tool calls in a row.")
                return
            await _checkpoint(db, run, usages)
            yield ("step", step_out(step))
            continue

        run.invalid_calls = 0
        run.tool_calls += 1
        arguments = args.model_dump(mode="json")

        if tool.kind == "write":
            approval = Approval(
                run_id=run.id,
                step_index=len(run.steps),
                tool_name=tool.name,
                arguments=arguments,
                arguments_hash=_arguments_hash(tool.name, arguments),
                expires_at=utcnow() + timedelta(hours=settings.agent_approval_ttl_hours),
            )
            run.approvals.append(approval)
            assert tool.preview is not None
            preview = tool.preview(args)
            step = _add_step(
                run,
                "approval",
                tool_name=tool.name,
                thought=decision.thought,
                arguments=arguments,
                summary=f"waiting for approval: {preview}",
            )
            run.status = AgentRunStatus.WAITING_APPROVAL
            await _checkpoint(db, run, usages)
            yield ("step", step_out(step))
            yield ("approval_required", approval_out(approval, preview))
            yield ("done", run_out(run))
            return

        began = time.monotonic()
        result, ok = await _execute(ctx, tool, args)
        _append(run, agent_prompt.tool_result(tool.name, _observation(result)))
        step = _add_step(
            run,
            "tool_call",
            tool_name=tool.name,
            thought=decision.thought,
            arguments=arguments,
            summary=result.summary[:500],
            ok=ok,
            duration_ms=round((time.monotonic() - began) * 1000),
        )
        await _checkpoint(db, run, usages)
        yield ("step", step_out(step))


# --------------------------------------------------------------------------- entry points
async def _default_scope(db: AsyncSession, user: User) -> list[uuid.UUID]:
    documents, _ = await document_service.list_documents(
        db, user, status=DocumentStatus.READY, limit=get_settings().agent_max_scope_documents
    )
    return [d.id for d in documents]


async def start_run(db: AsyncSession, user: User, goal: str, document_ids: list[uuid.UUID]) -> AsyncIterator[Event]:
    settings = get_settings()
    if document_ids:
        ids = list(dict.fromkeys(document_ids))
        await document_service.get_owned_documents(db, user, ids)  # 404 if any isn't theirs
    else:
        ids = await _default_scope(db, user)
    run = AgentRun(owner_id=user.id, goal=goal, document_ids=[str(i) for i in ids], status=AgentRunStatus.RUNNING)
    run.steps = []
    run.approvals = []
    db.add(run)
    await db.commit()
    yield ("run", {"id": str(run.id), "status": run.status})

    scope = await _scope_documents(db, user, run.document_ids)
    ctx = ToolContext(db=db, user=user, run=run, scope=scope)
    routed = await route_goal(goal, [d.title for d in scope.values()], ctx.usages)
    run.route, run.route_confidence, run.route_reason = routed.route, routed.confidence, routed.reason
    note = "" if routed.route == routed.picked else f" (picked {routed.picked} at {routed.confidence:.2f})"
    step = _add_step(run, "route", summary=f"{routed.route}{note}: {routed.reason}"[:500])
    tools = [t.spec() for name in ROUTE_TOOLS[routed.route] if (t := get_tool(name)) is not None]
    run.messages = agent_prompt.build_messages(
        goal, routed.route, [(str(d.id), d.title) for d in scope.values()], tools, settings.agent_max_steps
    )
    await _checkpoint(db, run, ctx.usages)
    yield ("step", step_out(step))
    async for event in _loop(db, user, run, ctx):
        yield event


async def get_run(db: AsyncSession, user: User, run_id: uuid.UUID) -> AgentRun:
    run = (
        await db.execute(select(AgentRun).where(AgentRun.id == run_id, AgentRun.owner_id == user.id))
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError("Agent run not found.", code="agent_run_not_found")
    return run


async def list_runs(db: AsyncSession, user: User, limit: int) -> list[AgentRun]:
    stmt = select(AgentRun).where(AgentRun.owner_id == user.id).order_by(AgentRun.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def decide(
    db: AsyncSession, user: User, run_id: uuid.UUID, approval_id: uuid.UUID, approve: bool
) -> AsyncIterator[Event]:
    """Approve or reject a pending write, then continue the run. Safe to call twice: the decision is a
    conditional update, so only the first call changes anything; later calls return the run as it is."""
    run = await get_run(db, user, run_id)
    if not any(a.id == approval_id for a in run.approvals):
        raise NotFoundError("Approval not found.", code="approval_not_found")
    with redis_lock(f"agent:run:{run.id}", get_settings().agent_timeout_seconds + 60) as acquired:
        if not acquired:
            raise ConflictError("This run is already being resumed.", code="run_busy")
        decided = (
            await db.execute(
                update(Approval)
                .where(
                    Approval.id == approval_id,
                    Approval.run_id == run.id,
                    Approval.status == ApprovalStatus.PENDING,
                    Approval.expires_at > utcnow(),
                )
                .values(
                    status=ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED,
                    decided_at=utcnow(),
                )
                .returning(Approval.id)
            )
        ).first()
        approval = (
            await db.execute(
                select(Approval).where(Approval.id == approval_id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        if decided is None:  # nothing changed in the database, so there is nothing to roll back
            if approval.status == ApprovalStatus.PENDING:
                raise ConflictError("This approval has expired; start a new run.", code="approval_expired")
            yield ("done", run_out(run))  # already decided: nothing to do
            return

        run.status = AgentRunStatus.RUNNING
        scope = await _scope_documents(db, user, run.document_ids)
        ctx = ToolContext(db=db, user=user, run=run, scope=scope)
        tool = get_tool(approval.tool_name)
        assert tool is not None
        if approve:
            # The stored arguments run, exactly as the user saw them; nothing from this request.
            began = time.monotonic()
            result, ok = await _execute(ctx, tool, tool.args_model.model_validate(approval.arguments))
            observation = f"The user approved. {_observation(result)}"
            summary, duration = f"approved: {result.summary}", round((time.monotonic() - began) * 1000)
        else:
            ok, duration = True, None
            observation = "The user rejected this action. Nothing was saved. Do not retry it; give your final answer."
            summary = "rejected by the user"
        _append(run, agent_prompt.tool_result(tool.name, observation))
        step = _add_step(
            run,
            "tool_call" if approve else "rejected",
            tool_name=tool.name,
            arguments=approval.arguments,
            summary=summary[:500],
            ok=ok,
            duration_ms=duration,
        )
        await _checkpoint(db, run, ctx.usages)
        yield ("step", step_out(step))
        async for event in _loop(db, user, run, ctx):
            yield event


async def list_outputs(db: AsyncSession, user: User, kind: str | None, limit: int) -> list[AgentOutput]:
    stmt = select(AgentOutput).where(AgentOutput.owner_id == user.id)
    if kind:
        stmt = stmt.where(AgentOutput.kind == kind)
    return list((await db.execute(stmt.order_by(AgentOutput.created_at.desc()).limit(limit))).scalars().all())
