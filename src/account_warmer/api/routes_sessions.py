"""Session list/detail routes for the dashboard (SPEC §2.17)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..core import repositories as repo
from .deps import get_db

logger = logging.getLogger("warmer.api")
router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("/{session_id}/stop")
def stop_session(session_id: int, db=Depends(get_db)):
    """Request a running session to stop.

    Sets ``stop_requested`` so the warming loop exits at the next video boundary and can
    flush its counters (SPEC §2.17). If the session looks stalled — no heartbeat for
    ``STALL_SECONDS`` (SPEC R21) — we close the row directly, since nothing is polling it.
    """
    from ..core.models import Session as SessionModel

    sess = db.get(SessionModel, session_id)
    if sess is None:
        raise HTTPException(404, f"session {session_id} not found")
    if sess.status != "running":
        raise HTTPException(400, f"session {session_id} is not running (status={sess.status})")

    repo.request_stop(db, session_id)

    if _is_stalled(sess):
        repo.close_session(db, sess, status="aborted", stop_reason="user_stop")
        logger.info("dashboard: session %s stopped by user (stalled — closed directly)", session_id)
        return {"id": sess.id, "status": sess.status, "stop_reason": sess.stop_reason,
                "mode": "closed", "stalled": True}

    logger.info("dashboard: stop requested for session %s", session_id)
    return {"id": session_id, "status": "running", "stop_reason": None,
            "mode": "graceful", "stalled": False}


#: A running session with no heartbeat for this long is considered stalled (SPEC R21).
STALL_SECONDS = 120.0


def _is_stalled(sess) -> bool:
    from ..utils.timeutil import parse_iso, utcnow

    hb = parse_iso(getattr(sess, "heartbeat_at", None))
    if hb is None:
        # Never heartbeated: give it the time its planned duration allows, plus slack.
        started = parse_iso(sess.started_at) or utcnow()
        planned = (getattr(sess, "planned_duration_s", None) or 0) + 120
        return (utcnow() - started).total_seconds() > planned
    return (utcnow() - hb).total_seconds() > STALL_SECONDS


@router.get("")
def list_sessions(
    account_id: int | None = None,
    limit: int = 50,
    db=Depends(get_db),
):
    """Recent sessions (optionally filtered by account)."""
    sessions = repo.recent_sessions(db, account_id=account_id, limit=min(limit, 200))
    return [
        {
            "id": s.id,
            "account_id": s.account_id,
            "status": s.status,
            "stop_reason": s.stop_reason,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "planned_duration_s": s.planned_duration_s,
            "actual_duration_s": s.actual_duration_s,
            "videos_watched": s.videos_watched,
            "ads_skipped": s.ads_skipped,
            "likes_given": s.likes_given,
            "comments_made": s.comments_made,
            "saves_done": s.saves_done,
            "follows_done": s.follows_done,
            "warnings_count": s.warnings_count,
            "errors_count": s.errors_count,
            "heartbeat_at": s.heartbeat_at,
            "stalled": _is_stalled(s) if s.status == "running" else False,
        }
        for s in sessions
    ]


@router.get("/{session_id}")
def session_detail(session_id: int, db=Depends(get_db)):
    """Single session with its watched videos."""
    from sqlalchemy import select
    from ..core.models import Session as SessionModel, WatchedVideo

    sess = db.get(SessionModel, session_id)
    if sess is None:
        raise HTTPException(404, f"session {session_id} not found")

    videos = list(db.scalars(
        select(WatchedVideo).where(WatchedVideo.session_id == session_id).limit(100)
    ).all())

    return {
        "id": sess.id,
        "account_id": sess.account_id,
        "status": sess.status,
        "stop_reason": sess.stop_reason,
        "started_at": sess.started_at,
        "ended_at": sess.ended_at,
        "planned_duration_s": sess.planned_duration_s,
        "actual_duration_s": sess.actual_duration_s,
        "videos_watched": sess.videos_watched,
        "ads_skipped": sess.ads_skipped,
        "likes_given": sess.likes_given,
        "comments_made": sess.comments_made,
        "saves_done": sess.saves_done,
        "follows_done": sess.follows_done,
        "warnings_count": sess.warnings_count,
        "errors_count": sess.errors_count,
        "fingerprint_hash": sess.fingerprint_hash,
        "proxy_used": sess.proxy_used,
        "videos": [
            {
                "video_key": v.video_key,
                "author_handle": v.author_handle,
                "watch_seconds": v.watch_seconds,
                "was_ad": bool(v.was_ad),
                "ad_skip_method": v.ad_skip_method,
                "watched_at": v.watched_at,
            }
            for v in videos
        ],
    }
