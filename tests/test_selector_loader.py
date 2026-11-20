"""Selector loader: schema validation of the real repo files + key extraction."""
from __future__ import annotations

import pytest

from account_warmer.adapters.selector_loader import (
    REQUIRED_GROUPS,
    PlatformSelectors,
    SelectorFileError,
    extract_video_key,
    load_platform_selectors,
    validate_spec,
)


class TestRealFiles:
    @pytest.mark.parametrize("platform", ["instagram", "tiktok"])
    def test_repo_selector_files_validate(self, app_root, platform):
        selectors = load_platform_selectors(platform, root=app_root)
        assert isinstance(selectors, PlatformSelectors)
        assert selectors.platform == platform
        assert selectors.schema_version >= 1

        # Every required group present and usable.
        for group in REQUIRED_GROUPS:
            if group == "bot_signals":
                subkeys = [k for k in selectors.groups if k.startswith("bot_signals.")]
                assert subkeys, "no bot_signal entries resolved"
                continue
            g = selectors.group(group)
            if group != "video_key_source":
                assert len(g.candidates) >= 1, f"{group} has no candidates"

        # base_urls non-empty; video key pattern compiles with a capture group.
        assert selectors.base_urls
        assert selectors.video_key_pattern.groups >= 1

    def test_ig_video_key_extraction(self, app_root):
        s = load_platform_selectors("instagram", root=app_root)
        assert extract_video_key(s, "https://www.instagram.com/reels/C1abcDEF456/?utm=x") == ("C1abcDEF456", None)
        assert extract_video_key(s, "https://www.instagram.com/") == (None, None)

    def test_tiktok_video_key_extraction(self, app_root):
        s = load_platform_selectors("tiktok", root=app_root)
        key, author = extract_video_key(
            s, "https://www.tiktok.com/@someuser/video/734567890123?is_from_webapp=1"
        )
        assert (key, author) == ("734567890123", "someuser")

    def test_text_patterns_case_insensitive(self, app_root):
        s = load_platform_selectors("instagram", root=app_root)
        ad_group = selectors_ad(s)
        assert any(p.search("sponsored content") for p in ad_group.compiled_text_patterns)


def selectors_ad(selectors: PlatformSelectors):
    return selectors.group("ad_indicators")


class TestValidationErrors:
    def test_missing_groups_reported_together(self, app_root):
        raw = load_platform_selectors("instagram", root=app_root)  # just to prove the file loads
        full = {
            "platform": "instagram",
            "schema_version": 2,
            "base_urls": {"reels": "https://www.instagram.com/reels/"},
        }
        for name in REQUIRED_GROUPS:
            if name == "bot_signals":
                full[name] = {"captcha": {"text_patterns": ["verify you are human"], "selectors": []}}
            elif name == "video_key_source":
                full[name] = {"url_pattern": "/reels/([A-Za-z0-9_-]+)"}
            else:
                full[name] = {"candidates": ["div"]}

        del full["like_button"]          # drop a candidate group...
        del full["logged_in_indicator"]  # ...and another one.

        with pytest.raises(SelectorFileError) as excinfo:
            validate_spec(full, expected_platform="instagram")
        joined = " ".join(excinfo.value.errors)
        assert "like_button" in joined
        assert "logged_in_indicator" in joined

    def test_empty_candidates_rejected(self):
        full = {
            "platform": "tiktok",
            "schema_version": 1,
            "base_urls": {"fyp": "https://www.tiktok.com/"},
        }
        for name in REQUIRED_GROUPS:
            if name == "bot_signals":
                full[name] = {"captcha": {"text_patterns": ["verify"]}}
            elif name == "video_key_source":
                full[name] = {"url_pattern": "/video/(\\d+)"}
            else:
                full[name] = {"candidates": ["div"]}

        full["feed_container"]["candidates"] = []  # must have >=1 candidate
        with pytest.raises(SelectorFileError) as excinfo:
            validate_spec(full, expected_platform="tiktok")
        assert any("feed_container" in e for e in excinfo.value.errors)

    def test_url_pattern_needs_capture_group(self):
        full = {
            "platform": "instagram",
            "schema_version": 1,
            "base_urls": {"home": "https://www.instagram.com/"},
        }
        for name in REQUIRED_GROUPS:
            if name == "bot_signals":
                full[name] = {"captcha": {"text_patterns": ["verify"]}}
            elif name == "video_key_source":
                full[name] = {"url_pattern": "/reels/ABC"}  # no capture group
            else:
                full[name] = {"candidates": ["div"]}

        with pytest.raises(SelectorFileError) as excinfo:
            validate_spec(full, expected_platform="instagram")
        assert any("capture group" in e for e in excinfo.value.errors)
