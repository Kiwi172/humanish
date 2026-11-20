"""Unit tests for core/scheduler.py — start-time sampling, duration, quick-check probability."""
from __future__ import annotations

import random
from datetime import time

import pytest

from account_warmer.core.scheduler import (
    DayPlan,
    SessionPlan,
    compute_video_budget,
    plan_account_day,
    sample_session_duration,
    seconds_until_start,
)


# ---------------------------------------------------------------------------
# Minimal config-like objects
# ---------------------------------------------------------------------------

class _DurationRange:
    min = 900
    max = 2700


class _WindowCfg:
    start = "08:00"
    end = "23:00"


class _DefaultsCfg:
    window = _WindowCfg()
    session_duration_s = _DurationRange()
    quick_check_probability = 0.15


class _SessionCfg:
    quick_check_duration_s = 300
    max_errors_before_abort = 5
    scroll_retry_on_fail = 1


class _RampRow:
    days_from = 0
    days_to = None
    videos_per_session = 20
    like_p = 0.12
    comment_p = 0.03
    sessions_per_day = 1


class _Cfg:
    defaults = _DefaultsCfg()
    session = _SessionCfg()

    def ramp_row(self, profile: str, age_days: int) -> _RampRow:
        return _RampRow()


CFG = _Cfg()


class _PlatformCfg:
    feed_modes = ["reels", "home"]
    feed_mode_weights = {"reels": 0.7, "home": 0.3}


PLATFORM_CFG = _PlatformCfg()


class _Account:
    window_start = None
    window_end = None
    ramp_profile = "default"
    created_at = "2026-01-01T00:00:00Z"  # old enough for age > 0


ACCOUNT = _Account()


# ---------------------------------------------------------------------------
# sample_session_duration
# ---------------------------------------------------------------------------

def test_sample_session_duration_within_range():
    rng = random.Random(42)
    for _ in range(500):
        d = sample_session_duration(rng, CFG)
        assert 900 <= d <= 2700


def test_sample_session_duration_skewed_short():
    """Triangular with mode at min+35% → mean should be below the midpoint."""
    rng = random.Random(7)
    samples = [sample_session_duration(rng, CFG) for _ in range(1000)]
    mean = sum(samples) / len(samples)
    midpoint = (900 + 2700) / 2  # 1800
    assert mean < midpoint


# ---------------------------------------------------------------------------
# plan_account_day
# ---------------------------------------------------------------------------

def test_plan_returns_dayplan():
    rng = random.Random(1)
    plan = plan_account_day(CFG, PLATFORM_CFG, ACCOUNT, rng=rng)
    assert isinstance(plan, DayPlan)
    assert len(plan.sessions) >= 1


# ---------------------------------------------------------------------------
# compute_video_budget (SPEC §2.12 ramp + daily video cap)
# ---------------------------------------------------------------------------

def test_video_budget_uses_ramp_when_no_cap():
    assert compute_video_budget(_RampRow()) == 20


def test_video_budget_clamps_to_remaining_day():
    """Daily cap already partly spent → budget shrinks to what's left."""
    budget = compute_video_budget(_RampRow(), daily_video_cap=25, videos_today=22)
    assert budget == 3


def test_video_budget_zero_when_cap_exhausted():
    assert compute_video_budget(_RampRow(), daily_video_cap=20, videos_today=20) == 0
    assert compute_video_budget(_RampRow(), daily_video_cap=20, videos_today=31) == 0


def test_video_budget_splits_across_remaining_sessions():
    """A 2-session day must not double its daily cap: each session gets half."""
    first = compute_video_budget(_RampRow(), daily_video_cap=20, videos_today=0,
                                 session_index=0, total_sessions=2)
    second = compute_video_budget(_RampRow(), daily_video_cap=20, videos_today=first,
                                  session_index=1, total_sessions=2)
    assert first == 10
    assert second == 10


def test_plan_populates_video_budget():
    rng = random.Random(3)
    plan = plan_account_day(CFG, PLATFORM_CFG, ACCOUNT, rng=rng,
                            daily_video_cap=25, videos_today=10)
    assert plan.sessions[0].video_budget == 15


def test_plan_video_budget_zero_at_cap():
    rng = random.Random(3)
    plan = plan_account_day(CFG, PLATFORM_CFG, ACCOUNT, rng=rng,
                            daily_video_cap=20, videos_today=20)
    assert all(s.video_budget == 0 for s in plan.sessions)


def test_plan_start_time_within_window():
    """Start time should be within the configured window (08:00–23:00)."""
    rng = random.Random(2)
    for _ in range(100):
        plan = plan_account_day(CFG, PLATFORM_CFG, ACCOUNT, rng=rng)
        st = plan.start_time
        assert time(8, 0) <= st <= time(23, 0)


def test_plan_feed_mode_from_config():
    """Feed mode should be one of the configured modes."""
    rng = random.Random(3)
    valid_modes = {"reels", "home"}
    for _ in range(50):
        plan = plan_account_day(CFG, PLATFORM_CFG, ACCOUNT, rng=rng)
        for s in plan.sessions:
            assert s.feed_mode in valid_modes


def test_plan_quick_check_shorter():
    """Quick-check sessions should have the configured short duration."""
    rng = random.Random(4)
    found_quick = False
    for _ in range(200):
        plan = plan_account_day(CFG, PLATFORM_CFG, ACCOUNT, rng=rng)
        for s in plan.sessions:
            if s.is_quick_check:
                found_quick = True
                assert s.duration_s == 300  # quick_check_duration_s
    assert found_quick  # with p=0.15 over 200 tries, we should see at least one


def test_plan_sessions_per_day():
    """Ramp row says sessions_per_day=1 → exactly one session."""
    rng = random.Random(5)
    for _ in range(20):
        plan = plan_account_day(CFG, PLATFORM_CFG, ACCOUNT, rng=rng)
        assert len(plan.sessions) == 1


# ---------------------------------------------------------------------------
# seconds_until_start
# ---------------------------------------------------------------------------

def test_seconds_until_start_positive_when_future():
    from datetime import datetime, timezone

    now = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    plan = DayPlan(start_time=time(14, 30), sessions=(SessionPlan(feed_mode="reels", duration_s=900),))
    delta = seconds_until_start(plan, timezone_id="UTC", now=now)
    # Should be ~4.5 hours in the future.
    assert 4 * 3600 < delta < 5 * 3600


def test_seconds_until_start_wraps_to_tomorrow():
    from datetime import datetime, timezone

    now = datetime(2026, 9, 15, 22, 0, tzinfo=timezone.utc)
    plan = DayPlan(start_time=time(8, 0), sessions=(SessionPlan(feed_mode="reels", duration_s=900),))
    delta = seconds_until_start(plan, timezone_id="UTC", now=now)
    # Should be ~10 hours (tomorrow morning).
    assert 9 * 3600 < delta < 11 * 3600


def test_seconds_until_start_timezone_aware():
    """A start time of 08:00 in America/New_York (UTC-4 in September) = 12:00 UTC."""
    from datetime import datetime, timezone

    # It's 10:00 UTC. In New York that's 06:00 local. Start at 08:00 local = 12:00 UTC.
    now = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    plan = DayPlan(start_time=time(8, 0), sessions=(SessionPlan(feed_mode="reels", duration_s=900),))
    delta = seconds_until_start(plan, timezone_id="America/New_York", now=now)
    # Should be ~2 hours (12:00 UTC - 10:00 UTC).
    assert 1.5 * 3600 < delta < 2.5 * 3600


def test_seconds_until_start_timezone_past_wraps():
    """If local time is past the start, it wraps to tomorrow."""
    from datetime import datetime, timezone

    # It's 14:00 UTC = 10:00 in New York. Start at 08:00 local already passed → tomorrow.
    now = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
    plan = DayPlan(start_time=time(8, 0), sessions=(SessionPlan(feed_mode="reels", duration_s=900),))
    delta = seconds_until_start(plan, timezone_id="America/New_York", now=now)
    # Tomorrow 08:00 NY = tomorrow 12:00 UTC. Delta ≈ 22 hours.
    assert 21 * 3600 < delta < 23 * 3600


def test_seconds_until_start_utc_plus_14():
    """Pacific timezone (UTC+14) — start at 09:00 local when it's 00:00 UTC."""
    from datetime import datetime, timezone

    # It's 00:00 UTC = 14:00 in Pacific/Kiritimati. Start 09:00 already passed → tomorrow.
    now = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)
    plan = DayPlan(start_time=time(9, 0), sessions=(SessionPlan(feed_mode="reels", duration_s=900),))
    delta = seconds_until_start(plan, timezone_id="Pacific/Kiritimati", now=now)
    # Tomorrow 09:00 +14 local = Sep 15 19:00 UTC. Delta from Sep 15 00:00 UTC = 19 hours.
    assert 18 * 3600 < delta < 20 * 3600
