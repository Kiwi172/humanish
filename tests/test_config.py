"""Config loading/validation against the real repo YAML files."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from account_warmer.core.config import (
    Pair,
    WarmerConfig,
    load_platform_config,
    load_warmer_config,
)


class TestRealConfigs:
    def test_warmer_yaml_validates(self, app_root):
        cfg = load_warmer_config(app_root)
        assert isinstance(cfg, WarmerConfig)
        # Spot-check values that the defaults depend on.
        assert 0 <= cfg.defaults.quick_check_probability <= 1
        assert cfg.defaults.session_duration_s.min < cfg.defaults.session_duration_s.max
        assert "default" in cfg.ramp_profiles

    def test_ramp_profile_row_selection(self, app_root):
        cfg = load_warmer_config(app_root)
        rows = cfg.ramp_profiles["default"]
        # Fresh account gets the lowest-volume row; mature account escalates.
        fresh = cfg.ramp_row("default", age_days=0)
        mature = cfg.ramp_row("default", age_days=120)
        assert mature.videos_per_session >= fresh.videos_per_session
        assert fresh in rows and mature in rows

    @pytest.mark.parametrize("platform", ["instagram", "tiktok"])
    def test_platform_configs_validate(self, app_root, platform):
        cfg = load_platform_config(platform, root=app_root)
        assert set(cfg.feed_mode_weights) == set(cfg.feed_modes)
        assert abs(sum(cfg.feed_mode_weights.values()) - 1.0) < 1e-9


class TestModelValidation:
    def test_pair_from_yaml_list(self):
        p = Pair.model_validate([30, 300])
        assert (p.low, p.high) == (30, 300)

    def test_pair_rejects_inverted_range(self):
        with pytest.raises(ValidationError):
            Pair.model_validate([300, 30])

    def test_unknown_key_fails_loud(self):
        """extra=forbid catches YAML typos (e.g. 'likey_p') instead of silently ignoring them."""
        raw = {
            "defaults": {"window": {"start": "08:00", "end": "23:00"},
                         "session_duration_s": {"min": 900, "max": 2700},
                         "quick_check_probability": 0.15},
            "session": {"quick_check_duration_s": 300, "max_errors_before_abort": 5,
                        "scroll_retry_on_fail": 1},
            "caps": {"daily_default": {"videos": 40, "likes": 12, "comments": 2,
                                       "saves": 4, "follows": 2}},
            "ramp_profiles": {"default": [
                {"days_from": 0, "days_to": 7, "videos_per_session": 12,
                 "like_p": 0.1, "comment_p": 0.0, "sessions_per_day": 1},
            ]},
            "behavior": {
                "likey_p": 0.15,  # typo on purpose
                "comment_p": 0.03, "save_p": 0.03, "follow_p": 0.008,
                "dwell_s": {"mean": 12, "sigma": 6, "min": 2, "max": 75,
                            "interact_multiplier_range": [1.4, 2.2], "glance_probability": 0.08},
                "idle_burst": {"probability_per_video": 0.06, "range_s": [30, 300]},
                "scroll": {"steps": 1, "step_height_range_px": [280, 640], "jitter_pct": 15,
                           "over_scroll_probability": 0.03, "back_scroll_probability": 0.02},
                "typing": {"wpm_range": [90, 160], "mid_typing_pause_probability": 0.35},
            },
        }
        with pytest.raises(ValidationError):
            WarmerConfig.model_validate(raw)

    def test_bad_window_bound_rejected(self):
        from account_warmer.core.config import WindowCfg

        with pytest.raises(ValidationError):
            WindowCfg.model_validate({"start": "25:00", "end": "23:00"})
