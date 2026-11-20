"""SQLAlchemy 2.x ORM models (SPEC §5). Mirrors the SQLite schema column-for-column.

Conventions:
- All timestamps are stored as UTC ISO-8601 **text** (``YYYY-MM-DDTHH:MM:SSZ``),
  defaulting via :func:`account_warmer.utils.timeutil.utcnow_iso`.
- Boolean-ish flags (``was_ad``) map to SQLite INTEGER per the DDL.
"""
from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..utils.timeutil import utcnow_iso


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------

class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("platform", "handle"),
        CheckConstraint("platform IN ('instagram','tiktok')"),
        CheckConstraint(
            "status IN ('new','needs_login','active','paused','flagged','error')"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(10), nullable=False)
    handle: Mapped[str] = mapped_column(Text, nullable=False)  # username without @
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    proxy_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone_id: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")  # IANA
    locale: Mapped[str] = mapped_column(Text, nullable=False, default="en-US")  # BCP-47
    device_profile: Mapped[str] = mapped_column(
        Text, nullable=False, default="android-chrome-pixel8"
    )
    window_start: Mapped[str | None] = mapped_column(String(5), nullable=True)  # HH:MM local
    window_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    daily_video_cap: Mapped[int | None] = mapped_column(nullable=True)
    daily_like_cap: Mapped[int | None] = mapped_column(nullable=True)
    daily_comment_cap: Mapped[int | None] = mapped_column(nullable=True)
    daily_save_cap: Mapped[int | None] = mapped_column(nullable=True)
    daily_follow_cap: Mapped[int | None] = mapped_column(nullable=True)
    ramp_profile: Mapped[str] = mapped_column(Text, nullable=False, default="default")
    # SPEC §2.10 "topic-tagged pools per account": JSON list of pool tags from
    # comments/<platform>_comments.yaml (e.g. ["food","travel"]); NULL/[] = general only.
    comment_tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_session_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )

    @property
    def display_name(self) -> str:
        return self.label or f"@{self.handle} ({self.platform})"


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

class Session(Base):  # noqa: F811 - table name wins over orm.Session in this module
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled','running','completed','aborted','error')"
        ),
        Index("idx_sessions_account_time", "account_id", text("started_at DESC")),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="scheduled")
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    ended_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    planned_duration_s: Mapped[int | None] = mapped_column(nullable=True)
    actual_duration_s: Mapped[int | None] = mapped_column(nullable=True)
    proxy_used: Mapped[str | None] = mapped_column(Text, nullable=True)  # redacted password in logs
    fingerprint_hash: Mapped[str | None] = mapped_column(Text, nullable=True)  # sha1(ua|tz|locale|profile)
    videos_watched: Mapped[int] = mapped_column(nullable=False, default=0)
    ads_skipped: Mapped[int] = mapped_column(nullable=False, default=0)
    likes_given: Mapped[int] = mapped_column(nullable=False, default=0)
    comments_made: Mapped[int] = mapped_column(nullable=False, default=0)
    saves_done: Mapped[int] = mapped_column(nullable=False, default=0)
    follows_done: Mapped[int] = mapped_column(nullable=False, default=0)
    warnings_count: Mapped[int] = mapped_column(nullable=False, default=0)
    errors_count: Mapped[int] = mapped_column(nullable=False, default=0)
    # SPEC R21: running sessions stamp this each loop iteration so the dashboard can show
    # a stalled session (machine sleep / hung page) instead of it looking healthy.
    heartbeat_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SPEC §2.17 stop button: set by the API, polled by the warming loop between videos.
    stop_requested: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


# ---------------------------------------------------------------------------
# watched_videos
# ---------------------------------------------------------------------------

class WatchedVideo(Base):
    __tablename__ = "watched_videos"
    __table_args__ = (
        Index("idx_watched_account_video", "account_id", "video_key"),
        Index("idx_watched_session", "session_id"),
        CheckConstraint(
            "ad_skip_method IN ('close_button','text_pattern','min_watch')"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    video_key: Mapped[str] = mapped_column(Text, nullable=False)  # stable id (shortcode / aweme id)
    author_handle: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)  # ~200 chars context
    watched_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    watch_seconds: Mapped[float] = mapped_column(nullable=False, default=0.0)
    was_ad: Mapped[bool] = mapped_column(nullable=False, default=False)  # INTEGER in SQLite
    ad_skip_method: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# actions (non-comment; dedupe via UNIQUE constraints)
# ---------------------------------------------------------------------------

class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        UniqueConstraint("account_id", "video_key", "action_type"),
        CheckConstraint("action_type IN ('like','save','follow')"),
        # Follows dedupe per target author regardless of video (partial unique index, SPEC §5).
        Index(
            "idx_actions_follow",
            "account_id",
            "target_handle",
            unique=True,
            sqlite_where=text("action_type = 'follow'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    video_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL for 'follow'
    target_handle: Mapped[str | None] = mapped_column(Text, nullable=True)  # set for 'follow'
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


# ---------------------------------------------------------------------------
# comments_history (own table: full text + template provenance)
# ---------------------------------------------------------------------------

class CommentHistory(Base):
    __tablename__ = "comments_history"
    __table_args__ = (UniqueConstraint("account_id", "video_key"),)  # max one per account+video

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    video_key: Mapped[str] = mapped_column(Text, nullable=False)
    author_handle: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_text: Mapped[str] = mapped_column(Text, nullable=False)
    template_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # pool/template id; NULL = manual
    posted_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


# ---------------------------------------------------------------------------
# event_logs (unified app events + errors + bot-detection warnings)
# ---------------------------------------------------------------------------

class EventLog(Base):
    __tablename__ = "event_logs"
    __table_args__ = (
        Index("idx_events_account_time", "account_id", text("created_at DESC")),
        Index("idx_events_level_cat", "level", "category"),
        CheckConstraint("level IN ('debug','info','warning','error')"),
        CheckConstraint(
            "category IN ('app','login','bot_signal','ad','selector','proxy','network','limit')"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True  # NULL = app-level
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    level: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="app")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string (failed candidates, url, screenshot…)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


# ---------------------------------------------------------------------------
# app_settings (dashboard-editable overrides; value is JSON-encoded)
# ---------------------------------------------------------------------------

class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


def all_models() -> list[type[Base]]:
    return [Account, Session, WatchedVideo, Action, CommentHistory, EventLog, AppSetting]
