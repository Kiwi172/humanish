"""Timezone-aware time helpers.

All DB timestamps are UTC ISO-8601 text (``YYYY-MM-DDTHH:MM:SSZ``); display layers
convert to the account's timezone (SPEC §5).
"""
from __future__ import annotations

import random
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """UTC now as the canonical DB timestamp string (default for all *_at columns)."""
    return utcnow().strftime(_ISO_FMT)


def parse_iso(value: str | datetime | None, *, assume_utc: bool = True) -> datetime | None:
    """Parse a stored ISO-8601 string into an aware UTC datetime.

    Naive strings are assumed UTC (``assume_utc=True``). Returns None for None input.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        # Accept trailing 'Z' and fractional seconds; fromisoformat (3.12) handles both mostly.
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        if assume_utc:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            raise ValueError(f"naive datetime {dt!r} and assume_utc=False")
    return dt.astimezone(timezone.utc)


def format_iso(dt: datetime | None) -> str | None:
    """Format an aware/naive datetime as the canonical DB string."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime(_ISO_FMT)


def parse_hhmm(value: str | time) -> time:
    """Parse ``HH:MM`` (or pass through a datetime.time) and validate the range."""
    if isinstance(value, time):
        return value
    m = _HHMM_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"not an HH:MM window bound: {value!r}")
    hh, mm = int(m.group(1)), int(m.group(2))
    return time(hh, mm)


def _t_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def day_bounds_utc(tz_name: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Start/end (UTC aware datetimes) of the calendar day in ``tz_name`` containing ``now``.

    Used for daily counters: "per UTC-local day of the account's timezone" (SPEC §5).
    Handles DST transitions correctly via ZoneInfo arithmetic.
    """
    tz = ZoneInfo(tz_name)
    now = parse_iso(now) or utcnow()
    local_now = now.astimezone(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_midnight.astimezone(timezone.utc)
    end_utc = (local_midnight + timedelta(days=1)).astimezone(timezone.utc)
    return start_utc, end_utc


def backoff_seconds(attempt: int, base: float = 60.0, cap: float = 3600.0) -> float:
    """Exponential backoff for session retry scheduling (attempt is 1-based)."""
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    return min(cap, base * (2 ** (attempt - 1)))


def sample_start_in_window(rng: random.Random, window_start: time, window_end: time) -> time:
    """Sample a session start inside [window_start, window_end) with a "natural hours" bias.

    Non-uniform by design (SPEC §6.4): a mixture of
      50% uniform across the window,
      30% triangular skewed toward an evening peak (~19:00, clamped inside the window),
      20% biased toward early in the window (triangular peaking at the start).
    Deterministic given ``rng`` (seeded) for tests.
    """
    w_start = _t_to_minutes(window_start)
    w_end = _t_to_minutes(window_end)
    if not 0 <= w_start < w_end <= 24 * 60:
        raise ValueError(f"invalid window {window_start}-{window_end}")

    span = w_end - w_start
    roll = rng.random()
    if roll < 0.5:
        minutes = rng.randint(w_start, max(w_start, w_end - 1))
    elif roll < 0.8:
        peak = min(max(19 * 60, w_start + span // 4), w_end - 1)
        minutes = int(rng.triangular(w_start, w_end - 1, peak))
    else:
        peak = w_start + max(span // 8, 5)
        minutes = int(rng.triangular(float(w_start), float(w_start + span * 0.6), float(peak)))

    minutes = min(max(minutes, w_start), w_end - 1)
    return time(minutes // 60, minutes % 60)
