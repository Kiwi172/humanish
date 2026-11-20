"""Query helpers over the ORM: account CRUD, dedupe checks, daily counters, event logs.

Conventions:
- Every function takes an **active** SQLAlchemy session as its first argument and
  *flushes* mutations (so PKs are available) but does not commit — callers own the
  transaction boundary via :func:`account_warmer.core.db.session_scope`.
- Dedupe relies on UNIQUE constraints as a race-safe backstop: ``add_action`` /
  ``add_comment`` run inside a SAVEPOINT and return ``None`` (== "already acted",
  not an error) when the constraint fires (SPEC §5).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as _OrmSession

from ..utils.timeutil import day_bounds_utc, format_iso, parse_iso, utcnow, utcnow_iso
from .models import (
    Account,
    Action,
    AppSetting,
    CommentHistory,
    EventLog,
    Session,
    WatchedVideo,
)


class DuplicateAccountError(ValueError):
    def __init__(self, platform: str, handle: str):
        super().__init__(f"account already exists: {platform}/{handle}")
        self.platform = platform
        self.handle = handle


def _detach_quietly(db: _OrmSession, obj) -> None:
    """Drop ``obj`` from the session if still attached.

    After a flush that fails inside a SAVEPOINT, SQLAlchemy may have already reverted
    the pending object to detached state — expunging it again would raise.
    """
    try:
        if obj in db:
            db.expunge(obj)
    except Exception:  # pragma: no cover - defensive across ORM states
        pass


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------

def create_account(
    db: _OrmSession,
    *,
    platform: str,
    handle: str,
    label: str | None = None,
    status: str = "new",
    proxy_url: str | None = None,
    timezone_id: str = "UTC",
    locale: str = "en-US",
    device_profile: str = "android-chrome-pixel8",
    window_start: str | None = None,
    window_end: str | None = None,
    daily_video_cap: int | None = None,
    daily_like_cap: int | None = None,
    daily_comment_cap: int | None = None,
    daily_save_cap: int | None = None,
    daily_follow_cap: int | None = None,
    ramp_profile: str = "default",
    comment_tags: list[str] | None = None,
    notes: str | None = None,
) -> Account:
    """Insert an account. Raises :class:`DuplicateAccountError` on (platform, handle)."""
    # Normalize ONCE so the duplicate check and the stored value can't diverge
    # ("@mybrand" must collide with "mybrand").
    handle = str(handle).strip().lstrip("@")
    if not handle:
        raise ValueError("handle is empty after trimming @/whitespace")

    existing = db.scalar(
        select(Account.id).where(Account.platform == platform, Account.handle == handle)
    )
    if existing is not None:
        raise DuplicateAccountError(platform, handle)

    account = Account(
        platform=platform,
        handle=handle,
        label=label,
        status=status,
        proxy_url=proxy_url,
        timezone_id=timezone_id,
        locale=locale,
        device_profile=device_profile,
        window_start=window_start,
        window_end=window_end,
        daily_video_cap=daily_video_cap,
        daily_like_cap=daily_like_cap,
        daily_comment_cap=daily_comment_cap,
        daily_save_cap=daily_save_cap,
        daily_follow_cap=daily_follow_cap,
        ramp_profile=ramp_profile,
        comment_tags=json.dumps(list(comment_tags), ensure_ascii=False) if comment_tags else None,
        notes=notes,
    )
    db.add(account)
    db.flush()
    return account


def account_comment_tags(account) -> list[str]:
    """Parse an account's ``comment_tags`` JSON column into a list (SPEC §2.10).

    Returns [] when unset/blank/unparseable — callers then fall back to the "general" pool.
    """
    raw = getattr(account, "comment_tags", None)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, str):
        return [data]
    if isinstance(data, list):
        return [str(t) for t in data if isinstance(t, str) and t]
    return []


def get_account(db: _OrmSession, account_id: int) -> Account | None:
    return db.get(Account, account_id)


def list_accounts(db: _OrmSession) -> list[Account]:
    return list(db.scalars(select(Account).order_by(Account.id)).all())


def update_account(account: Account, **fields: Any) -> Account:
    """Set arbitrary column values (e.g. status/proxy/caps) and bump ``updated_at``."""
    for key, value in fields.items():
        if not hasattr(account, key):
            raise ValueError(f"unknown Account field: {key}")
        setattr(account, key, value)
    account.updated_at = utcnow_iso()
    state = getattr(account, "_sa_instance_state", None)
    session = getattr(state, "session", None)  # None when detached (e.g. ad-hoc tests)
    if session is not None:
        session.flush()
    return account


def remove_account(db: _OrmSession, account_id: int) -> bool:
    account = db.get(Account, account_id)
    if account is None:
        return False
    db.delete(account)
    db.flush()
    return True


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

def create_session(
    db: _OrmSession,
    *,
    account_id: int,
    planned_duration_s: int | None = None,
    proxy_used: str | None = None,
    fingerprint_hash: str | None = None,
) -> Session:
    session = Session(
        account_id=account_id,
        status="scheduled",
        planned_duration_s=planned_duration_s,
        proxy_used=proxy_used,
        fingerprint_hash=fingerprint_hash,
    )
    db.add(session)
    db.flush()
    return session


def start_session(db: _OrmSession, session: Session) -> Session:
    now = utcnow_iso()
    session.status = "running"
    session.started_at = now
    account = db.get(Account, session.account_id)
    if account is not None:
        account.last_session_at = now
    db.flush()
    return session


def close_session(
    db: _OrmSession,
    session: Session,
    *,
    status: str,
    stop_reason: str | None = None,
    actual_duration_s: int | None = None,
) -> Session:
    if status not in ("completed", "aborted", "error"):
        raise ValueError(f"invalid close status: {status}")
    session.status = status
    session.stop_reason = stop_reason
    session.ended_at = utcnow_iso()
    session.actual_duration_s = actual_duration_s
    db.flush()
    return session


def get_running_session(db: _OrmSession, account_id: int) -> Session | None:
    """One writer per account (SPEC §3): the running session row is the lock."""
    return db.scalar(
        select(Session).where(Session.account_id == account_id, Session.status == "running")
    )


def touch_heartbeat(db: _OrmSession, session_id: int) -> Session | None:
    """Stamp ``heartbeat_at`` on a running session (SPEC R21 stall visibility)."""
    sess = db.get(Session, session_id)
    if sess is None or sess.status != "running":
        return None
    sess.heartbeat_at = utcnow_iso()
    db.flush()
    return sess


def request_stop(db: _OrmSession, session_id: int) -> Session | None:
    """Ask a running session to stop at the next loop boundary (SPEC §2.17 stop button)."""
    sess = db.get(Session, session_id)
    if sess is None or sess.status != "running":
        return None
    sess.stop_requested = 1
    db.flush()
    return sess


def stop_requested(db: _OrmSession, session_id: int) -> bool:
    """Whether the operator requested a stop for this session (polled by the warming loop)."""
    value = db.scalar(
        select(Session.stop_requested).where(Session.id == session_id)
    )
    return bool(value)


def recent_sessions(db: _OrmSession, account_id: int | None = None, limit: int = 20) -> list[Session]:
    stmt = select(Session).order_by(Session.started_at.desc().nullslast(), Session.id.desc())
    if account_id is not None:
        stmt = stmt.where(Session.account_id == account_id)
    return list(db.scalars(stmt.limit(limit)).all())


# ---------------------------------------------------------------------------
# watched videos
# ---------------------------------------------------------------------------

def record_watched(
    db: _OrmSession,
    *,
    account_id: int,
    video_key: str,
    watch_seconds: float = 0.0,
    session_id: int | None = None,
    author_handle: str | None = None,
    caption_snippet: str | None = None,
    was_ad: bool = False,
    ad_skip_method: str | None = None,
) -> WatchedVideo:
    """One row per video-view (SPEC §5 storage note)."""
    if caption_snippet is not None:
        caption_snippet = caption_snippet[:200]
    watched = WatchedVideo(
        session_id=session_id,
        account_id=account_id,
        video_key=video_key,
        author_handle=author_handle,
        caption_snippet=caption_snippet,
        watch_seconds=watch_seconds,
        was_ad=bool(was_ad),
        ad_skip_method=ad_skip_method,
    )
    db.add(watched)
    db.flush()
    return watched


# ---------------------------------------------------------------------------
# dedupe checks + actions (SPEC §2.11 / §5)
# ---------------------------------------------------------------------------

def _action_exists(db: _OrmSession, account_id: int, video_key: str | None, action_type: str) -> bool:
    stmt = select(Action.id).where(
        Action.account_id == account_id, Action.action_type == action_type
    )
    if video_key is not None:
        stmt = stmt.where(Action.video_key == video_key)
    return db.scalar(stmt.limit(1)) is not None


def already_liked(db: _OrmSession, account_id: int, video_key: str) -> bool:
    return _action_exists(db, account_id, video_key, "like")


def already_saved(db: _OrmSession, account_id: int, video_key: str) -> bool:
    return _action_exists(db, account_id, video_key, "save")


def already_followed(db: _OrmSession, account_id: int, target_handle: str) -> bool:
    row = db.scalar(
        select(Action.id)
        .where(Action.account_id == account_id, Action.action_type == "follow", Action.target_handle == target_handle)
        .limit(1)
    )
    return row is not None


def already_commented(db: _OrmSession, account_id: int, video_key: str) -> bool:
    row = db.scalar(
        select(CommentHistory.id)
        .where(CommentHistory.account_id == account_id, CommentHistory.video_key == video_key)
        .limit(1)
    )
    return row is not None


def add_action(
    db: _OrmSession,
    *,
    account_id: int,
    action_type: str,
    video_key: str | None = None,
    target_handle: str | None = None,
    session_id: int | None = None,
) -> Action | None:
    """Insert an action row. Returns ``None`` when a UNIQUE constraint already covered it
    (duplicate like/save/follow → treat as "already acted", not an error)."""
    if action_type not in ("like", "save", "follow"):
        raise ValueError(f"invalid action_type: {action_type}")
    row = Action(
        session_id=session_id,
        account_id=account_id,
        video_key=None if action_type == "follow" else video_key,
        target_handle=target_handle if action_type == "follow" else None,
        action_type=action_type,
    )
    try:
        with db.begin_nested():  # SAVEPOINT — keeps the outer transaction usable
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        _detach_quietly(db, row)
        return None


def add_comment(
    db: _OrmSession,
    *,
    account_id: int,
    video_key: str,
    comment_text: str,
    author_handle: str | None = None,
    template_key: str | None = None,
    session_id: int | None = None,
) -> CommentHistory | None:
    """Insert a comment row. Returns ``None`` when the (account, video) unique fired."""
    row = CommentHistory(
        session_id=session_id,
        account_id=account_id,
        video_key=video_key,
        author_handle=author_handle,
        comment_text=comment_text,
        template_key=template_key,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        _detach_quietly(db, row)
        return None


# ---------------------------------------------------------------------------
# daily counters (per account-local calendar day, SPEC §5)
# ---------------------------------------------------------------------------

def _day_bounds_str(account_timezone_id: str, now: datetime | None = None) -> tuple[str, str]:
    start_utc, end_utc = day_bounds_utc(account_timezone_id, now)
    return format_iso(start_utc), format_iso(end_utc)


def count_today(
    db: _OrmSession,
    account: Account,
    kind: str,
    now: datetime | None = None,
) -> int:
    """Daily counter for ``kind`` in the account's local timezone.

    kind ∈ {video, ads, like, save, follow, comment}. ISO strings compare
    lexicographically == chronologically (uniform zero-padded format).
    """
    start_s, end_s = _day_bounds_str(account.timezone_id, now)
    if kind in ("like", "save", "follow"):
        stmt = select(func.count()).select_from(Action).where(
            Action.account_id == account.id,
            Action.action_type == kind,
            Action.created_at >= start_s,
            Action.created_at < end_s,
        )
    elif kind == "comment":
        stmt = select(func.count()).select_from(CommentHistory).where(
            CommentHistory.account_id == account.id,
            CommentHistory.posted_at >= start_s,
            CommentHistory.posted_at < end_s,
        )
    elif kind == "video":
        stmt = select(func.count()).select_from(WatchedVideo).where(
            WatchedVideo.account_id == account.id,
            WatchedVideo.was_ad.is_(False),
            WatchedVideo.watched_at >= start_s,
            WatchedVideo.watched_at < end_s,
        )
    elif kind == "ads":
        stmt = select(func.count()).select_from(WatchedVideo).where(
            WatchedVideo.account_id == account.id,
            WatchedVideo.was_ad.is_(True),
            WatchedVideo.watched_at >= start_s,
            WatchedVideo.watched_at < end_s,
        )
    else:
        raise ValueError(f"unknown counter kind: {kind}")
    return int(db.scalar(stmt) or 0)


def warnings_in_window(
    db: _OrmSession,
    account_id: int | None = None,
    *,
    level: str = "warning",
    category: str = "bot_signal",
    within_hours: float = 48.0,
    now: datetime | None = None,
) -> int:
    """Count recent bot-signal warnings (drives ``warnings.pause_after`` auto-flag)."""
    now = parse_iso(now) or utcnow()
    bound = format_iso(now - timedelta(hours=within_hours))
    stmt = select(func.count()).select_from(EventLog).where(
        EventLog.level == level,
        EventLog.category == category,
        EventLog.created_at >= bound,
    )
    if account_id is not None:
        stmt = stmt.where(EventLog.account_id == account_id)
    return int(db.scalar(stmt) or 0)


# ---------------------------------------------------------------------------
# stats aggregates (CLI `stats` / dashboard Step 4)
# ---------------------------------------------------------------------------

def period_totals(
    db: _OrmSession,
    *,
    account_id: int | None = None,
    since_iso: str | None = None,
) -> dict[str, int]:
    """Totals over a trailing window (default: last 24 h). ``since_iso`` is inclusive."""
    if since_iso is None:
        since_iso = format_iso(utcnow() - timedelta(hours=24))

    def _count(stmt):
        return int(db.scalar(stmt) or 0)

    totals: dict[str, int] = {}
    wv_base = select(func.count()).select_from(WatchedVideo).where(WatchedVideo.watched_at >= since_iso)
    if account_id is not None:
        wv_base = wv_base.where(WatchedVideo.account_id == account_id)
    totals["videos"] = _count(wv_base.where(WatchedVideo.was_ad.is_(False)))
    totals["ads"] = _count(wv_base.where(WatchedVideo.was_ad.is_(True)))

    for kind in ("like", "save", "follow"):
        stmt = select(func.count()).select_from(Action).where(
            Action.created_at >= since_iso, Action.action_type == kind
        )
        if account_id is not None:
            stmt = stmt.where(Action.account_id == account_id)
        totals[kind] = _count(stmt)

    cstmt = select(func.count()).select_from(CommentHistory).where(
        CommentHistory.posted_at >= since_iso
    )
    if account_id is not None:
        cstmt = cstmt.where(CommentHistory.account_id == account_id)
    totals["comments"] = _count(cstmt)

    esbase = select(func.count()).select_from(EventLog).where(
        EventLog.created_at >= since_iso, EventLog.level.in_(("warning", "error"))
    )
    if account_id is not None:
        esbase = esbase.where(EventLog.account_id == account_id)
    totals["warnings"] = _count(esbase.where(EventLog.level == "warning"))
    totals["errors"] = _count(esbase.where(EventLog.level == "error"))

    sstmt = select(func.count()).select_from(Session).where(
        Session.started_at >= since_iso, Session.status.in_(("completed", "aborted", "error"))
    )
    if account_id is not None:
        sstmt = sstmt.where(Session.account_id == account_id)
    totals["sessions"] = _count(sstmt)

    return totals


def query_events(
    db: _OrmSession,
    *,
    level: str | None = None,
    category: str | None = None,
    account_id: int | None = None,
    limit: int = 50,
) -> list[EventLog]:
    """Filtered event log query (CLI `warmer logs`). Newest first."""
    stmt = select(EventLog).order_by(EventLog.created_at.desc(), EventLog.id.desc())
    if level is not None:
        stmt = stmt.where(EventLog.level == level)
    if category is not None:
        stmt = stmt.where(EventLog.category == category)
    if account_id is not None:
        stmt = stmt.where(EventLog.account_id == account_id)
    return list(db.scalars(stmt.limit(limit)).all())


def log_event(
    db: _OrmSession,
    *,
    level: str,
    message: str,
    category: str = "app",
    account_id: int | None = None,
    session_id: int | None = None,
    detail: dict[str, Any] | str | None = None,
) -> EventLog:
    """Insert an event log row. ``detail`` dicts are JSON-encoded on write."""
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False)
    event = EventLog(
        account_id=account_id,
        session_id=session_id,
        level=level,
        category=category,
        message=message,
        detail=detail,
    )
    db.add(event)
    db.flush()
    return event


# ---------------------------------------------------------------------------
# selector health / drift tracking (SPEC §7.2 rule 2)
# ---------------------------------------------------------------------------

def record_selector_matches(
    db: _OrmSession,
    *,
    account_id: int,
    session_id: int | None,
    matches: list[dict[str, Any]],
) -> None:
    """Store per-group match results for one session (for cross-session drift alarms).

    ``matches`` = [{"group": str, "matched_index": int|None}, ...] — one entry per group.
    Stored as JSON in event_logs with category='selector', level='debug'.
    """
    if not matches:
        return
    log_event(
        db,
        level="debug",
        category="selector",
        message=f"selector match record for session {session_id}",
        account_id=account_id,
        session_id=session_id,
        detail={"matches": matches},
    )


def selector_drift_warnings(
    db: _OrmSession,
    *,
    account_id: int,
    threshold: int = 5,
    window: int = 10,
) -> list[str]:
    """Check the last ``window`` sessions for drift: primary candidate (index 0) failing
    in >= ``threshold`` of them while a fallback matched → 'selectors may be stale'."""
    import json as _json

    events = db.scalars(
        select(EventLog)
        .where(
            EventLog.account_id == account_id,
            EventLog.category == "selector",
            EventLog.level == "debug",
        )
        .order_by(EventLog.created_at.desc(), EventLog.id.desc())
        .limit(window)
    ).all()

    if len(events) < threshold:
        return []

    # Aggregate per-group fallback hits across the window.
    from collections import defaultdict
    group_fallbacks: dict[str, int] = defaultdict(int)
    for ev in events:
        try:
            detail = _json.loads(ev.detail) if ev.detail else {}
        except (_json.JSONDecodeError, TypeError):
            continue
        for m in detail.get("matches", []):
            idx = m.get("matched_index")
            if idx is not None and idx > 0:
                group_fallbacks[m["group"]] += 1

    warnings = [
        f"{g}: primary candidate failing ({n}/{len(events)} sessions on fallback)"
        for g, n in group_fallbacks.items()
        if n >= threshold
    ]
    return warnings


# ---------------------------------------------------------------------------
# app_settings (dashboard overrides; value is JSON-encoded per SPEC §5)
# ---------------------------------------------------------------------------

def _decode_setting(raw: str | None) -> Any:
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - manual edits happen
        return raw


def get_setting(db: _OrmSession, key: str) -> Any | None:
    row = db.get(AppSetting, key)
    if row is None:
        return None
    return _decode_setting(row.value)


def set_setting(db: _OrmSession, key: str, value: Any) -> AppSetting:
    row = db.get(AppSetting, key)
    payload = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    if row is None:
        row = AppSetting(key=key, value=payload)
        db.add(row)
    else:
        row.value = payload
    db.flush()
    return row
