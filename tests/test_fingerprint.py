"""Fingerprint presets, option building, and audit-hash stability."""
from __future__ import annotations

import types

import pytest

from account_warmer.core.fingerprint import (
    FINGERPRINT_PRESETS,
    build_fingerprint,
    get_preset,
)


def _account(device_profile="android-chrome-pixel8", tz="America/New_York", locale="en-US"):
    """Minimal stand-in for an ORM Account row."""
    return types.SimpleNamespace(timezone_id=tz, locale=locale, device_profile=device_profile)


class TestPresets:
    def test_default_preset_is_android_chrome(self):
        assert "android-chrome-pixel8" in FINGERPRINT_PRESETS
        p = get_preset("android-chrome-pixel8")
        assert not p.flagged
        assert "Chrome/" in p.user_agent and "Android" in p.user_agent

    def test_iphone_preset_is_flagged(self):
        p = get_preset("iphone-15-safari")
        assert p.flagged  # Safari UA on Blink — opt-in only (SPEC R4)
        assert "Safari" in p.user_agent or "AppleWebKit" in p.user_agent

    def test_unknown_preset_lists_valid_names(self):
        with pytest.raises(ValueError, match="unknown device_profile"):
            get_preset("windows-edge-surface")


class TestBuildFingerprint:
    def test_options_and_viewport(self):
        fp = build_fingerprint(_account())
        assert fp.user_agent.startswith("Mozilla/5.0 (Linux; Android 14; Pixel 8)")
        assert fp.viewport == {"width": 412, "height": 915}
        assert fp.screen == fp.viewport  # phones: no desktop chrome
        assert fp.is_mobile and fp.has_touch
        assert fp.timezone_id == "America/New_York"
        assert fp.locale == "en-US"

    def test_hash_stable_for_same_identity(self):
        h1 = build_fingerprint(_account()).fingerprint_hash
        h2 = build_fingerprint(_account()).fingerprint_hash
        assert h1 == h2 and len(h1) == 40  # sha1 hexdigest

    def test_hash_changes_with_timezone(self):
        h1 = build_fingerprint(_account(tz="America/New_York")).fingerprint_hash
        h2 = build_fingerprint(_account(tz="Europe/Berlin")).fingerprint_hash
        assert h1 != h2  # identity audit: tz is part of the fingerprint

    def test_hash_changes_with_device_profile(self):
        h1 = build_fingerprint(_account()).fingerprint_hash
        h2 = build_fingerprint(_account(device_profile="android-galaxy-a54")).fingerprint_hash
        assert h1 != h2
