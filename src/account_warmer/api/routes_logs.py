"""Filtered event log query for the dashboard (SPEC §2.17)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core import repositories as repo
from .deps import get_db

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("")
def list_logs(
    level: str | None = None,
    category: str | None = None,
    account_id: int | None = None,
    limit: int = 100,
    db=Depends(get_db),
):
    """Filtered event log (newest first)."""
    events = repo.query_events(
        db,
        level=level,
        category=category,
        account_id=account_id,
        limit=min(limit, 500),
    )
    return [
        {
            "id": e.id,
            "account_id": e.account_id,
            "session_id": e.session_id,
            "level": e.level,
            "category": e.category,
            "message": e.message,
            "detail": e.detail,
            "created_at": e.created_at,
        }
        for e in events
    ]
