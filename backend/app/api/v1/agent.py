import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agent import runner
from app.core.errors import AppError, NotFoundError, error_responses
from app.core.logging import get_logger
from app.core.rate_limit import rate_limited
from app.db.session import get_async_sessionmaker, get_db
from app.models import User
from app.schemas.agent import AgentOutputOut, AgentRunCreate, AgentRunList, AgentRunOut, ApprovalDecision

logger = get_logger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(rate_limited("default"))]
AgentUser = Annotated[User, Depends(rate_limited("chat"))]


async def _until_done(events: AsyncIterator[tuple[str, dict[str, Any]]]) -> AgentRunOut:
    done: dict[str, Any] | None = None
    async for event, data in events:
        if event == "done":
            done = data
    assert done is not None, "an agent run always ends with a done event"
    return AgentRunOut.model_validate(done)


@router.post(
    "/runs",
    responses=error_responses(404),
    response_model=AgentRunOut,
    summary="Run the agent on a goal (waits until it finishes or pauses for approval)",
    description="The router picks a route, the agent calls tools step by step and answers with [n] "
    "citations. A write action (save a report, create a task) pauses the run with `status: "
    "waiting_approval` and a `pending_approval`; nothing is written until you approve it.",
)
async def create_run(body: AgentRunCreate, db: DB, user: AgentUser) -> AgentRunOut:
    return await _until_done(runner.start_run(db, user, body.goal, body.document_ids))


@router.post(
    "/runs/stream",
    responses=error_responses(404),
    summary="Run the agent and stream its steps (Server-Sent Events)",
    description="Events: `run` (its id) → `step` (one per route, tool call or approval) → optional "
    "`approval_required` → `done` (the run, same shape as POST /agent/runs) or `error`.",
    response_class=EventSourceResponse,
)
async def create_run_stream(body: AgentRunCreate, db: DB, user: AgentUser) -> EventSourceResponse:
    user_id = user.id
    await db.close()  # the stream opens its own session (see chat.ask_stream for why)

    async def events() -> AsyncIterator[dict[str, Any]]:
        async with get_async_sessionmaker()() as stream_db:
            try:
                stream_user = await stream_db.get(User, user_id)
                if stream_user is None:
                    raise NotFoundError("User not found.", code="user_not_found")
                async for event, data in runner.start_run(stream_db, stream_user, body.goal, body.document_ids):
                    yield {"event": event, "data": json.dumps(data, default=str)}
            except AppError as exc:
                yield {"event": "error", "data": json.dumps({"code": exc.code, "message": exc.message})}
            except Exception:
                logger.exception("agent_stream_failed")
                yield {
                    "event": "error",
                    "data": json.dumps({"code": "internal_error", "message": "The agent run failed."}),
                }

    return EventSourceResponse(events(), ping=15)


@router.get("/runs", response_model=AgentRunList, summary="Your recent agent runs, newest first")
async def list_runs(db: DB, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> AgentRunList:
    runs = await runner.list_runs(db, user, limit)
    return AgentRunList(items=[AgentRunOut.model_validate(runner.run_out(r, with_steps=False)) for r in runs])


@router.get("/runs/{run_id}", response_model=AgentRunOut, summary="One agent run with its steps")
async def get_run(run_id: uuid.UUID, db: DB, user: CurrentUser) -> AgentRunOut:
    return AgentRunOut.model_validate(runner.run_out(await runner.get_run(db, user, run_id)))


@router.post(
    "/runs/{run_id}/approvals/{approval_id}",
    responses=error_responses(409),
    response_model=AgentRunOut,
    summary="Approve or reject a pending action, then continue the run",
    description="Idempotent: only the first decision counts; repeating it returns the run unchanged. "
    "On approve, the arguments stored with the approval run (exactly what you were shown). "
    "409 `run_busy` if the run is being resumed elsewhere, `approval_expired` after 24 hours.",
)
async def decide(
    run_id: uuid.UUID, approval_id: uuid.UUID, body: ApprovalDecision, db: DB, user: AgentUser
) -> AgentRunOut:
    return await _until_done(runner.decide(db, user, run_id, approval_id, body.decision == "approve"))


@router.get("/outputs", response_model=list[AgentOutputOut], summary="Reports and tasks the agent saved for you")
async def list_outputs(
    db: DB,
    user: CurrentUser,
    kind: Literal["report", "task"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AgentOutputOut]:
    return [AgentOutputOut.model_validate(o) for o in await runner.list_outputs(db, user, kind, limit)]
