"""Starting a comparison from the API side (the job itself is in services/comparison.py, worker side).

Used by POST /comparisons and by the agent's compare_documents tool, so both check ownership and
readiness the same way and never leave a pending row that nothing will run.
"""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotReadyError, QueueUnavailableError
from app.core.logging import get_logger
from app.models import Comparison, User
from app.models.enums import DocumentStatus
from app.services.documents import get_owned_documents

logger = get_logger(__name__)


async def create_comparison(db: AsyncSession, user: User, document_ids: list[uuid.UUID], focus: str) -> Comparison:
    from app.workers import tasks  # the worker module imports services; import it late to avoid a cycle

    ids = list(dict.fromkeys(document_ids))
    documents = await get_owned_documents(db, user, ids)
    not_ready = [str(d.id) for d in documents if d.status != DocumentStatus.READY]
    if not_ready:
        raise NotReadyError("All documents must be ready before comparing.", details={"not_ready": not_ready})
    comparison = Comparison(owner_id=user.id, document_ids=[str(i) for i in ids], focus=" ".join(focus.split()))
    db.add(comparison)
    await db.commit()
    await db.refresh(comparison)
    try:
        await asyncio.to_thread(tasks.enqueue_comparison, comparison.id)
    except Exception as exc:
        logger.exception("enqueue_failed", comparison_id=str(comparison.id))
        await db.delete(comparison)  # nothing will run it; don't leave a row "pending" forever
        await db.commit()
        raise QueueUnavailableError("The job queue is unavailable right now. Please try again shortly.") from exc
    return comparison
