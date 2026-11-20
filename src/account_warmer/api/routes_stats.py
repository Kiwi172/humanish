"""Stats aggregates for dashboard charts (SPEC §2.17)."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func

from ..core import repositories as repo
from ..core.models import Session
from ..utils.timeutil import format_iso, utcnow
from .deps import get_db

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/overview")
def overview(db=Depends(get_db)):
    """Dashboard top: 24h totals + active sessions count."""
    totals_24h = repo.period_totals(db)
    running = db.query(func.count()).select_from(Session).filter_by(status="running").scalar() or 0
    return {
        "totals_24h": totals_24h,
        "active_sessions": int(running),
    }


@router.get("/daily")
def daily_series(days: int = 7, account_id: int | None = None, db=Depends(get_db)):
    """Per-day video/action counts for the last N days (chart data)."""
    from sqlalchemy import func, select

    from ..core.models import Action, CommentHistory, WatchedVideo

    now = utcnow()
    rows: list[dict] = []
    for i in range(days - 1, -1, -1):
        day_end = now - timedelta(days=i)
        day_start = day_end - timedelta(days=1)
        start_s, end_s = format_iso(day_start), format_iso(day_end)

        def _count(stmt):
            return int(db.scalar(stmt) or 0)

        wv_base = select(func.count()).select_from(WatchedVideo).where(
            WatchedVideo.watched_at >= start_s, WatchedVideo.watched_at < end_s
        )
        if account_id is not None:
            wv_base = wv_base.where(WatchedVideo.account_id == account_id)

        videos = _count(wv_base.where(WatchedVideo.was_ad.is_(False)))
        ads = _count(wv_base.where(WatchedVideo.was_ad.is_(True)))

        a_base = select(func.count()).select_from(Action).where(
            Action.created_at >= start_s, Action.created_at < end_s
        )
        if account_id is not None:
            a_base = a_base.where(Action.account_id == account_id)
        likes = _count(a_base.where(Action.action_type == "like"))
        saves = _count(a_base.where(Action.action_type == "save"))

        c_stmt = select(func.count()).select_from(CommentHistory).where(
            CommentHistory.posted_at >= start_s, CommentHistory.posted_at < end_s
        )
        if account_id is not None:
            c_stmt = c_stmt.where(CommentHistory.account_id == account_id)
        comments = _count(c_stmt)

        rows.append({
            "date": day_start.strftime("%Y-%m-%d"),
            "videos": videos,
            "ads": ads,
            "likes": likes,
            "saves": saves,
            "comments": comments,
        })
    return rows
