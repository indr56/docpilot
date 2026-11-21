import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, error_responses
from app.core.logging import get_logger
from app.core.rate_limit import rate_limited
from app.db.session import get_db
from app.models import Comparison, User
from app.models.enums import ComparisonStatus
from app.schemas.chat import ComparisonCreate, ComparisonOut
from app.services import comparison_requests, events, jobs

logger = get_logger(__name__)
router = APIRouter(prefix="/comparisons", tags=["comparisons"])

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(rate_limited("default"))]
ChatUser = Annotated[User, Depends(rate_limited("chat"))]


@router.post(
    "",
    responses=error_responses(404, 409, 503),
    response_model=ComparisonOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Compare 2-5 documents (runs in the background)",
    description="Returns immediately with `status: pending`. Poll GET /comparisons/{id} or listen for "
    "`comparison.completed` on the events WebSocket.",
)
async def create_comparison(body: ComparisonCreate, db: DB, user: ChatUser) -> ComparisonOut:
    comparison = await comparison_requests.create_comparison(db, user, body.document_ids, body.focus)
    return ComparisonOut.model_validate(comparison)


@router.get("", response_model=list[ComparisonOut], summary="Your 50 most recent comparisons, newest first")
async def list_comparisons(db: DB, user: CurrentUser) -> list[ComparisonOut]:
    rows = (
        (
            await db.execute(
                select(Comparison)
                .where(Comparison.owner_id == user.id)
                .order_by(Comparison.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return [ComparisonOut.model_validate(c) for c in rows]


@router.get("/{comparison_id}", response_model=ComparisonOut, summary="One comparison: status and result")
async def get_comparison(comparison_id: uuid.UUID, db: DB, user: CurrentUser) -> ComparisonOut:
    comparison = (
        await db.execute(select(Comparison).where(Comparison.id == comparison_id, Comparison.owner_id == user.id))
    ).scalar_one_or_none()
    if comparison is None:
        raise NotFoundError("Comparison not found.", code="comparison_not_found")
    return ComparisonOut.model_validate(comparison)


@router.delete(
    "/{comparison_id}",
    responses=error_responses(409),
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a comparison",
    description="Refused (409) while it is running: the worker is about to write its result.",
)
async def delete_comparison(comparison_id: uuid.UUID, db: DB, user: CurrentUser) -> Response:
    comparison = (
        await db.execute(select(Comparison).where(Comparison.id == comparison_id, Comparison.owner_id == user.id))
    ).scalar_one_or_none()
    if comparison is None:
        raise NotFoundError("Comparison not found.", code="comparison_not_found")
    if comparison.status == ComparisonStatus.RUNNING or await asyncio.to_thread(
        jobs.is_locked, jobs.comparison_lock_key(comparison.id)
    ):
        raise ConflictError("The comparison is running. Delete it once it finishes.", code="comparison_running")
    await db.delete(comparison)  # a pending job finds no row and skips
    await db.commit()
    await events.apublish(user.id, "comparison.deleted", {"comparison_id": comparison_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
