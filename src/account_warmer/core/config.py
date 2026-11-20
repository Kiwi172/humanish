"""Config loading: ``.env`` (secrets/paths) + ``config/*.yaml`` tuning with Pydantic v2 validation.

Resolution order for paths: explicit argument > env var > project-root default.
The *project root* is discovered by walking up from the cwd until ``pyproject.toml``
is found (or overridden via the ``WARMER_ROOT`` env var / CLI option).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------
# Project root / env
# --------------------------------------------------------------------------

def find_project_root(start: str | Path | None = None) -> Path:
    """Locate the project root (directory containing ``pyproject.toml``)."""
    cur = Path(start or os.environ.get("WARMER_ROOT") or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise FileNotFoundError(
        f"could not locate project root: no pyproject.toml found at or above {cur}"
    )


def load_env(root: str | Path) -> dict[str, str]:
    """Load ``<root>/.env`` into os.environ (if present) and return it."""
    env_path = Path(root) / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    return dict(os.environ)


def data_dir_from_env(root: str | Path) -> Path:
    raw = os.environ.get("WARMER_DATA_DIR", "data")
    p = Path(raw).expanduser()
    return p if p.is_absolute() else Path(root) / p


def db_path_from_env(root: str | Path) -> Path:
    override = os.environ.get("WARMER_DB_PATH")
    if override:
        p = Path(override).expanduser()
        return p if p.is_absolute() else Path(root) / p
    return data_dir_from_env(root) / "warmer.db"


def log_dir_from_env(root: str | Path) -> Path:
    # Logs live next to the data dir by default (both gitignored).
    raw = os.environ.get("WARMER_LOG_DIR")
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else Path(root) / p
    return data_dir_from_env(root).parent / "logs"


# --------------------------------------------------------------------------
# Shared validation helpers
# --------------------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


# --------------------------------------------------------------------------
# warmer.yaml models (SPEC §10)
# --------------------------------------------------------------------------

class Pair(_Strict):
    """A validated [min, max] pair from YAML (e.g. ``idle range_s: [30, 300]``)."""

    low: float = Field(gt=0)
    high: float

    @model_validator(mode="before")
    @classmethod
    def _from_list(cls, data: Any) -> Any:
        if isinstance(data, (list, tuple)) and len(data) == 2:
            return {"low": data[0], "high": data[1]}
        return data

    @model_validator(mode="after")
    def _check_order(self) -> "Pair":
        _require(self.high >= self.low, f"range high ({self.high}) < low ({self.low})")
        return self


class WindowCfg(_Strict):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _check_hhmm(cls, v: str) -> str:
        from ..utils.timeutil import parse_hhmm  # local import avoids cycle at module load

        parse_hhmm(v)
        return v

    @model_validator(mode="after")
    def _check_order(self) -> "WindowCfg":
        """The active window must run forward — a reversed window breaks the scheduler."""
        from ..utils.timeutil import parse_hhmm

        start, end = parse_hhmm(self.start), parse_hhmm(self.end)
        _require(
            (start.hour, start.minute) < (end.hour, end.minute),
            f"window end ({self.end}) must be after start ({self.start})",
        )
        return self


class DurationRange(_Strict):
    min: int = Field(gt=0)
    max: int

    @model_validator(mode="after")
    def _check(self) -> "DurationRange":
        _require(self.max >= self.min, f"max ({self.max}) < min ({self.min})")
        return self


class DefaultsCfg(_Strict):
    window: WindowCfg
    session_duration_s: DurationRange
    quick_check_probability: float = Field(ge=0, le=1)


class SessionCfg(_Strict):
    quick_check_duration_s: int = Field(gt=0)
    max_errors_before_abort: int = Field(ge=1)
    scroll_retry_on_fail: int = Field(ge=0)


class DwellCfg(_Strict):
    mean: float = Field(gt=0)
    sigma: float = Field(ge=0)
    min: float = Field(gt=0)
    max: float
    interact_multiplier_range: Pair
    glance_probability: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> "DwellCfg":
        _require(self.max >= self.min, f"max ({self.max}) < min ({self.min})")
        return self


class IdleBurstCfg(_Strict):
    probability_per_video: float = Field(ge=0, le=1)
    range_s: Pair


class ScrollCfg(_Strict):
    steps: int = Field(ge=1)
    step_height_range_px: Pair
    jitter_pct: float = Field(ge=0, le=100)
    over_scroll_probability: float = Field(ge=0, le=1)
    back_scroll_probability: float = Field(ge=0, le=1)


class TypingCfg(_Strict):
    wpm_range: Pair
    mid_typing_pause_probability: float = Field(ge=0, le=1)


class BehaviorCfg(_Strict):
    like_p: float = Field(ge=0, le=1)
    comment_p: float = Field(ge=0, le=1)
    save_p: float = Field(ge=0, le=1)
    follow_p: float = Field(ge=0, le=1)
    dwell_s: DwellCfg
    idle_burst: IdleBurstCfg
    scroll: ScrollCfg
    typing: TypingCfg


class CapsDailyDefault(_Strict):
    videos: int = Field(ge=0)
    likes: int = Field(ge=0)
    comments: int = Field(ge=0)
    saves: int = Field(ge=0)
    follows: int = Field(ge=0)


class CapsCfg(_Strict):
    daily_default: CapsDailyDefault


class RampRow(_Strict):
    days_from: int = Field(ge=0)
    days_to: int | None = Field(default=None, ge=1)
    videos_per_session: int = Field(gt=0)
    like_p: float = Field(ge=0, le=1)
    comment_p: float = Field(ge=0, le=1)
    sessions_per_day: int = Field(ge=1)

    @model_validator(mode="after")
    def _check(self) -> "RampRow":
        if self.days_to is not None:
            _require(self.days_to > self.days_from, f"days_to ({self.days_to}) <= days_from ({self.days_from})")
        return self


class PauseAfter(_Strict):
    count: int = Field(ge=1)
    within_hours: int = Field(ge=1)


class WarningsCfg(_Strict):
    pause_after: PauseAfter
    screenshots_on_warning: bool = True


class WarmerConfig(_Strict):
    """Validated shape of ``config/warmer.yaml``."""

    defaults: DefaultsCfg
    session: SessionCfg
    behavior: BehaviorCfg
    caps: CapsCfg
    ramp_profiles: dict[str, list[RampRow]]
    warnings: WarningsCfg = Field(default_factory=lambda: WarningsCfg(
        pause_after=PauseAfter(count=2, within_hours=48), screenshots_on_warning=True))

    @field_validator("ramp_profiles")
    @classmethod
    def _check_profiles(cls, v: dict[str, list[RampRow]]) -> dict[str, list[RampRow]]:
        _require(len(v) > 0, "at least one ramp profile required")
        _require("default" in v, "'default' ramp profile is required (accounts.ramp_profile references it)")
        for name, rows in v.items():
            _require(len(rows) > 0, f"ramp profile {name!r} has no rows")
        return v

    def ramp_row(self, profile: str, age_days: int) -> RampRow:
        """Return the row applicable to an account of ``age_days`` under ``profile``."""
        rows = self.ramp_profiles.get(profile) or self.ramp_profiles["default"]
        for row in sorted(rows, key=lambda r: r.days_from):
            if age_days >= row.days_from and (row.days_to is None or age_days < row.days_to):
                return row
        # Age beyond the last bounded row: use the final row.
        return max(rows, key=lambda r: r.days_from)


# --------------------------------------------------------------------------
# config/<platform>.yaml models
# --------------------------------------------------------------------------

class ScrollGestureCfg(_Strict):
    swipe_duration_ms_range: Pair
    hold_at_edge_ms_range: Pair


PLATFORMS = ("instagram", "tiktok")


class PlatformConfig(_Strict):
    """Validated shape of ``config/instagram.yaml`` / ``config/tiktok.yaml``."""

    platform: str
    feed_modes: list[str]
    feed_mode_weights: dict[str, float]
    extra_ad_text_patterns: list[str] = Field(default_factory=list)
    scroll_gesture: ScrollGestureCfg

    @field_validator("platform")
    @classmethod
    def _check_platform(cls, v: str) -> str:
        _require(v in PLATFORMS, f"unknown platform {v!r}; expected one of {PLATFORMS}")
        return v

    @field_validator("feed_modes")
    @classmethod
    def _check_modes(cls, v: list[str]) -> list[str]:
        _require(len(v) > 0 and len(set(v)) == len(v), "feed_modes must be a non-empty unique list")
        return v

    @model_validator(mode="after")
    def _check_weights(self) -> "PlatformConfig":
        modes = set(self.feed_modes)
        weights = self.feed_mode_weights
        _require(set(weights) == modes, f"feed_mode_weights keys {sorted(weights)} != feed_modes {sorted(modes)}")
        total = sum(weights.values())
        _require(abs(total - 1.0) < 0.01, f"feed_mode_weights must sum to ~1.0 (got {total})")
        for k, w in weights.items():
            _require(w > 0, f"weight for {k!r} must be > 0")
        return self


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def load_warmer_config(root: str | Path | None = None) -> WarmerConfig:
    """Load + validate ``config/warmer.yaml``. Raises on schema problems (typos fail loud)."""
    root = find_project_root(root)
    path = root / "config" / "warmer.yaml"
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path} (run `warmer init` or check out the repo)")
    return WarmerConfig.model_validate(_read_yaml(path))


def load_platform_config(platform: str, root: str | Path | None = None) -> PlatformConfig:
    """Load + validate ``config/<platform>.yaml``."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}; expected one of {PLATFORMS}")
    root = find_project_root(root)
    path = root / "config" / f"{platform}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    return PlatformConfig.model_validate(_read_yaml(path))
