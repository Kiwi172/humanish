"""Account list/detail/create/status routes for the dashboard (SPEC §2.17)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..core import repositories as repo
from .deps import get_db, get_root

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    platform: str
    handle: str
    label: str | None = None
    timezone_id: str = "UTC"
    locale: str = "en-US"
    device_profile: str = "android-chrome-pixel8"
    proxy_url: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    daily_like_cap: int | None = None
    daily_comment_cap: int | None = None
    comment_tags: list[str] | None = None


@router.get("")
def list_accounts(request: Request, db=Depends(get_db)):
    """All accounts with today's counters vs caps."""
    from ..services.stats_service import resolve_effective_caps
    from ..core.settings_overrides import load_config_with_overrides

    cfg = load_config_with_overrides(get_root(request))
    accounts = repo.list_accounts(db)
    rows = []
    for a in accounts:
        caps = resolve_effective_caps(a, cfg)
        counters = {
            "videos": repo.count_today(db, a, "video"),
            "likes": repo.count_today(db, a, "like"),
            "comments": repo.count_today(db, a, "comment"),
            "saves": repo.count_today(db, a, "save"),
            "follows": repo.count_today(db, a, "follow"),
        }
        rows.append({
            "id": a.id,
            "platform": a.platform,
            "handle": a.handle,
            "label": a.label,
            "status": a.status,
            "timezone_id": a.timezone_id,
            "proxy_url": _redact(a.proxy_url),
            "window": f"{a.window_start or '—'}–{a.window_end or '—'}",
            "caps": caps,
            "counters_today": counters,
            "last_session_at": a.last_session_at,
        })
    return rows


@router.get("/{account_id}")
def account_detail(account_id: int, request: Request, db=Depends(get_db)):
    """Single account with recent sessions + actions."""
    from ..services.stats_service import resolve_effective_caps
    from ..core.settings_overrides import load_config_with_overrides

    cfg = load_config_with_overrides(get_root(request))
    a = repo.get_account(db, account_id)
    if a is None:
        raise HTTPException(404, f"account {account_id} not found")

    caps = resolve_effective_caps(a, cfg)
    counters = {
        "videos": repo.count_today(db, a, "video"),
        "likes": repo.count_today(db, a, "like"),
        "comments": repo.count_today(db, a, "comment"),
        "saves": repo.count_today(db, a, "save"),
        "follows": repo.count_today(db, a, "follow"),
    }
    sessions = repo.recent_sessions(db, account_id=account_id, limit=10)
    events = repo.query_events(db, account_id=account_id, limit=20)
    drift_warnings = repo.selector_drift_warnings(db, account_id=a.id)

    return {
        "id": a.id,
        "platform": a.platform,
        "handle": a.handle,
        "label": a.label,
        "status": a.status,
        "timezone_id": a.timezone_id,
        "locale": a.locale,
        "device_profile": a.device_profile,
        "proxy_url": _redact(a.proxy_url),
        "window_start": a.window_start,
        "window_end": a.window_end,
        "ramp_profile": a.ramp_profile,
        "comment_tags": repo.account_comment_tags(a),
        "caps": caps,
        "counters_today": counters,
        "last_login_at": a.last_login_at,
        "last_session_at": a.last_session_at,
        "notes": a.notes,
        "created_at": a.created_at,
        "sessions": [
            {
                "id": s.id,
                "status": s.status,
                "stop_reason": s.stop_reason,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "planned_duration_s": s.planned_duration_s,
                "actual_duration_s": s.actual_duration_s,
                "videos_watched": s.videos_watched,
                "likes_given": s.likes_given,
                "comments_made": s.comments_made,
            }
            for s in sessions
        ],
        "recent_events": [
            {
                "id": e.id,
                "level": e.level,
                "category": e.category,
                "message": e.message,
                "created_at": e.created_at,
            }
            for e in events
        ],
        "selector_drift_warnings": drift_warnings,
    }


@router.post("")
def create_account(body: AccountCreate, request: Request, db=Depends(get_db)):
    """Add a new account from the dashboard form."""
    try:
        acct = repo.create_account(
            db,
            platform=body.platform,
            handle=body.handle,
            label=body.label,
            timezone_id=body.timezone_id,
            locale=body.locale,
            device_profile=body.device_profile,
            proxy_url=body.proxy_url,
            window_start=body.window_start,
            window_end=body.window_end,
            daily_like_cap=body.daily_like_cap,
            daily_comment_cap=body.daily_comment_cap,
            comment_tags=body.comment_tags,
        )
    except repo.DuplicateAccountError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": acct.id, "handle": acct.handle}


@router.patch("/{account_id}/status")
def set_status(account_id: int, status: str, request: Request, db=Depends(get_db)):
    """Change account status (pause/resume/flag/etc.)."""
    valid = ("new", "needs_login", "active", "paused", "flagged", "error")
    if status not in valid:
        raise HTTPException(400, f"status must be one of {valid}")
    a = repo.get_account(db, account_id)
    if a is None:
        raise HTTPException(404, f"account {account_id} not found")
    repo.update_account(a, status=status)
    return {"id": a.id, "status": a.status}


def _redact(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    from ..browser.proxy import redact_proxy

    return redact_proxy(proxy_url)
