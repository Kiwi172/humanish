"""Session planning: daily start time, duration sampling, quick-check decisions (SPEC §3/§6.4).

Hand-rolled asyncio scheduling — no APScheduler in v1. Given the validated
:class:`~account_warmer.core.config.WarmerConfig` + an account row this module produces a
:class:`DayPlan` / :class:`SessionPlan`; all randomness flows through an injected
``random.Random`` so tests can seed it deterministically.

Public API:
    plan_account_day(cfg, platform_cfg, account, now) -> DayPlan | None
    sample_session_duration(rng, cfg) -> int
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from ..utils.timeutil import parse_hhmm, sample_start_in_window, utcnow


@dataclass(frozen=True)
class SessionPlan:
    """One concrete warming session to run."""

    feed_mode: str                 # "reels"|"home" (IG) / "fyp" (TikTok) — weighted pick
    duration_s: int                # sampled within config range (or quick-check length)
    is_quick_check: bool = False
    video_budget: int = 0          # max non-ad videos to watch (ramp + daily cap, SPEC §2.12)


@dataclass(frozen=True)
class DayPlan:
    """What an account does on one local day."""

    start_time: time               # sampled inside the active window (biased, not uniform)
    sessions: tuple[SessionPlan, ...]  # usually 1; ramp rows may allow more


def _window_for(account, cfg) -> tuple[time, time]:
    """Account-local daily window: per-account override > global default."""
    start = parse_hhmm(account.window_start or cfg.defaults.window.start)
    end = parse_hhmm(account.window_end or cfg.defaults.window.end)
    return start, end


def _pick_feed_mode(rng: random.Random, platform_cfg) -> str:
    """Weighted feed-mode selection from config/<platform>.yaml."""
    modes = platform_cfg.feed_modes
    weights = [platform_cfg.feed_mode_weights[m] for m in modes]
    return rng.choices(modes, weights=weights, k=1)[0]


def sample_session_duration(rng: random.Random, cfg) -> int:
    """Sample a full-session duration (seconds) inside the configured range.

    Skewed toward the shorter end — most human sessions are brief; long ones happen but
    are less common (SPEC §6.4 "non-uniform delays everywhere").
    """
    d = cfg.defaults.session_duration_s
    return int(rng.triangular(d.min, d.max, d.min + 0.35 * (d.max - d.min)))


def compute_video_budget(
    ramp_row,
    *,
    daily_video_cap: int | None = None,
    videos_today: int = 0,
    session_index: int = 0,
    total_sessions: int = 1,
) -> int:
    """How many non-ad videos one session may watch (SPEC §2.12 ramp + daily cap).

    The ramp profile's ``videos_per_session`` sets the per-session budget; the account's
    effective daily video cap (per-account override > global default) clamps the *day*,
    split across the day's sessions so a 2-session day doesn't double its cap. Returns 0
    when the day's cap is already spent — the caller should then skip the session.
    """
    per_session = int(ramp_row.videos_per_session)
    if daily_video_cap is None:
        return max(0, per_session)

    remaining_day = max(0, int(daily_video_cap) - max(0, int(videos_today)))
    sessions_left = max(1, int(total_sessions) - max(0, int(session_index)))
    return max(0, min(per_session, remaining_day // sessions_left))


def plan_account_day(
    cfg,
    platform_cfg,
    account,
    now: datetime | None = None,
    *,
    rng: random.Random | None = None,
    daily_video_cap: int | None = None,
    videos_today: int = 0,
) -> DayPlan | None:
    """Build the day's plan for one account.

    Returns ``None`` when nothing is due today (already planned past its session count —
    callers track that via last_session_at; this function only decides *shape*). The ramp
    profile row for the account's age determines sessions-per-day and per-session video
    budget context; quick-check days replace the full session with a short one.
    """
    rng = rng or random.Random()
    now = now or utcnow()

    w_start, w_end = _window_for(account, cfg)
    start_time = sample_start_in_window(rng, w_start, w_end)

    # Account age in days (from created_at; new rows default to today → age 0).
    from ..utils.timeutil import parse_iso

    created = parse_iso(getattr(account, "created_at", None)) or now
    age_days = max(0, int((now - created).total_seconds() // 86400))
    ramp_row = cfg.ramp_row(account.ramp_profile, age_days)

    sessions: list[SessionPlan] = []
    total = ramp_row.sessions_per_day
    for idx in range(total):
        budget = compute_video_budget(
            ramp_row,
            daily_video_cap=daily_video_cap,
            videos_today=videos_today,
            session_index=idx,
            total_sessions=total,
        )
        if rng.random() < cfg.defaults.quick_check_probability and not sessions:
            # Quick-check day: one short session instead of the full one (SPEC §6.4).
            sessions.append(
                SessionPlan(
                    feed_mode=_pick_feed_mode(rng, platform_cfg),
                    duration_s=cfg.session.quick_check_duration_s,
                    is_quick_check=True,
                    video_budget=budget,
                )
            )
        else:
            sessions.append(
                SessionPlan(
                    feed_mode=_pick_feed_mode(rng, platform_cfg),
                    duration_s=sample_session_duration(rng, cfg),
                    video_budget=budget,
                )
            )

    return DayPlan(start_time=start_time, sessions=tuple(sessions))


def seconds_until_start(
    day_plan: DayPlan,
    timezone_id: str = "UTC",
    now: datetime | None = None,
) -> float:
    """Seconds from ``now`` (UTC) until the planned local start time in the account's timezone.

    Converts the wall-clock start_time into a UTC instant using the account's IANA timezone
    (DST-aware via ZoneInfo). Returns the delta to today's occurrence, or tomorrow's if it
    already passed locally.
    """
    from zoneinfo import ZoneInfo

    now = now or utcnow()
    tz = ZoneInfo(timezone_id)

    # What is "today" in the account's local timezone?
    local_now = now.astimezone(tz)
    target_date = local_now.date()

    # Build the candidate datetime in the account's local wall clock, then convert to UTC.
    candidate_local = datetime(
        target_date.year, target_date.month, target_date.day,
        day_plan.start_time.hour, day_plan.start_time.minute,
        tzinfo=tz,
    )
    # If it already passed today locally, try tomorrow.
    if candidate_local <= local_now:
        candidate_local += timedelta(days=1)

    return (candidate_local - now).total_seconds()
