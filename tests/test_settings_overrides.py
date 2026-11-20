"""Tests for core/settings_overrides.py (SPEC §10 live dashboard overrides) and
core/behavior.build_gesture_params (SPEC §10 scroll/typing config → adapter params)."""
from __future__ import annotations

import pytest

from account_warmer.core.behavior import build_gesture_params
from account_warmer.core.config import load_platform_config, load_warmer_config
from account_warmer.core.settings_overrides import OVERRIDABLE, apply_overrides


@pytest.fixture()
def cfg(app_root):
    return load_warmer_config(app_root)


# ---------------------------------------------------------------------------
# apply_overrides
# ---------------------------------------------------------------------------

def test_override_daily_cap(cfg):
    applied = apply_overrides(cfg, {"caps.daily_default.likes": 99})
    assert applied == ["caps.daily_default.likes"]
    assert cfg.caps.daily_default.likes == 99


def test_override_from_json_string_value(cfg):
    """Settings rows store JSON text — both decoded and raw forms are accepted."""
    apply_overrides(cfg, {"behavior.like_p": "0.42"})
    assert cfg.behavior.like_p == pytest.approx(0.42)


def test_override_window_times(cfg):
    apply_overrides(cfg, {"defaults.window.start": "06:30", "defaults.window.end": "22:00"})
    assert cfg.defaults.window.start == "06:30"
    assert cfg.defaults.window.end == "22:00"


def test_override_screenshots_flag(cfg):
    apply_overrides(cfg, {"warnings.screenshots_on_warning": False})
    assert cfg.warnings.screenshots_on_warning is False


def test_unknown_key_ignored(cfg):
    before = cfg.model_dump()
    applied = apply_overrides(cfg, {"ramp_profiles.default": "evil", "not.a.path": 1})
    assert applied == []
    assert cfg.model_dump() == before


def test_invalid_value_rejected_and_config_unchanged(cfg):
    """A bad manual edit must not corrupt the validated config."""
    before = cfg.model_dump()
    applied = apply_overrides(cfg, {"caps.daily_default.likes": "not-a-number"})
    assert applied == []
    assert cfg.model_dump() == before


def test_inconsistent_override_rejected(cfg):
    """An override that breaks a cross-field validator is skipped (window end < start)."""
    before = cfg.model_dump()
    applied = apply_overrides(cfg, {"defaults.window.end": "01:00"})  # start is 08:00
    assert applied == []
    assert cfg.defaults.window.end == before["defaults"]["window"]["end"]


def test_bool_not_accepted_for_numeric_field(cfg):
    applied = apply_overrides(cfg, {"caps.daily_default.likes": True})
    assert applied == []


def test_coerces_int_to_float(cfg):
    apply_overrides(cfg, {"behavior.comment_p": 0})
    assert isinstance(cfg.behavior.comment_p, float)
    assert cfg.behavior.comment_p == 0.0


def test_all_overridable_paths_are_real(cfg):
    """Every whitelisted path must resolve on the real config shape (no typos)."""
    for path in OVERRIDABLE:
        obj = cfg
        for part in path.split("."):
            obj = getattr(obj, part, None)
            assert obj is not None, f"path {path!r} does not resolve"


# ---------------------------------------------------------------------------
# build_gesture_params (SPEC §10: scroll/typing config actually reaches adapters)
# ---------------------------------------------------------------------------

def test_gesture_params_from_real_configs(app_root, cfg):
    platform_cfg = load_platform_config("instagram", app_root)
    params = build_gesture_params(cfg, platform_cfg)

    assert params.steps == cfg.behavior.scroll.steps
    assert params.step_height_min_px == int(cfg.behavior.scroll.step_height_range_px.low)
    assert params.step_height_max_px == int(cfg.behavior.scroll.step_height_range_px.high)
    assert params.jitter_pct == cfg.behavior.scroll.jitter_pct
    assert params.over_scroll_probability == cfg.behavior.scroll.over_scroll_probability
    assert params.back_scroll_probability == cfg.behavior.scroll.back_scroll_probability
    # Platform scroll_gesture wins for gesture timing.
    assert params.swipe_duration_min_ms == int(platform_cfg.scroll_gesture.swipe_duration_ms_range.low)
    assert params.swipe_duration_max_ms == int(platform_cfg.scroll_gesture.swipe_duration_ms_range.high)
    assert params.hold_at_edge_min_ms == int(platform_cfg.scroll_gesture.hold_at_edge_ms_range.low)
    # Typing realism comes from warmer.yaml behavior.typing.
    assert params.typing_wpm_min == float(cfg.behavior.typing.wpm_range.low)
    assert params.typing_wpm_max == float(cfg.behavior.typing.wpm_range.high)
    assert params.typing_pause_probability == cfg.behavior.typing.mid_typing_pause_probability


def test_gesture_params_without_platform_cfg(cfg):
    params = build_gesture_params(cfg, None)
    assert params.swipe_duration_min_ms == 160  # documented defaults
    assert params.steps >= 1


def test_gesture_params_defaults_are_valid():
    from account_warmer.adapters.base import GestureParams

    params = GestureParams()
    assert params.step_height_max_px >= params.step_height_min_px


def test_gesture_params_rejects_inverted_ranges():
    from account_warmer.adapters.base import GestureParams

    with pytest.raises(ValueError):
        GestureParams(step_height_min_px=600, step_height_max_px=200)
