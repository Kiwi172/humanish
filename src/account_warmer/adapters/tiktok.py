"""TikTok browser adapter (SPEC §7 — all UI access via injected selectors, no raw CSS).

Feed mode: ``fyp`` (For You page). "Save" maps to TikTok's bookmark action. Video key +
author come from the URL pattern (``/@author/video/<id>``) via video_key_source.
"""
from __future__ import annotations

import asyncio
import logging
import random

from .base import AdInfo, BotSignal, GestureParams, PlatformAdapter, VideoRef
from .dom import SelectorHealth, match_text_patterns, resolve_first_visible, visible_text
from .selector_loader import extract_video_key
from . import scrolling

logger = logging.getLogger("warmer")


class TikTokAdapter(PlatformAdapter):
    FEED_MODES = ("fyp",)

    def __init__(self, page, selectors, *, rng: random.Random | None = None,
                 params: GestureParams | None = None):
        super().__init__(page, selectors, params=params)
        self._mode: str | None = None
        self.rng = rng or random.Random()
        self.health = SelectorHealth()
        # Platform extra ad patterns (config/tiktok.yaml), compiled + injected by service.
        self.extra_ad_patterns: tuple = ()
        # How the last scroll ended, for logging: None | "overshoot_bounce" | "rewatch".
        self.last_nav_variant: str | None = None

    # -- feed navigation ----------------------------------------------------
    async def open_feed(self, mode: str) -> None:
        if mode not in self.FEED_MODES:
            raise ValueError(f"unknown TikTok feed mode {mode!r}; expected one of {self.FEED_MODES}")
        url = self.selectors.base_urls.get(mode)
        if not url:
            raise LookupError(f"no base_url for feed mode {mode!r}")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        rec = await resolve_first_visible(self.page, "feed_container", self.selectors.group("feed_container").candidates)
        self.health.record(rec.group, rec.matched_index, rec.matched_selector)
        if rec.matched_index is None:
            raise RuntimeError("TikTok feed container did not resolve (all candidates failed)")

    async def current_video(self) -> VideoRef | None:
        url = self.page.url or ""
        video_key, author = extract_video_key(self.selectors, url)
        if not video_key:
            return None
        caption = await visible_text(self.page, "[data-e2e='feed-item']")[:200] or None
        return VideoRef(video_key=video_key, author_handle=author, caption_snippet=caption)

    async def _current_key(self) -> str:
        ref = await self.current_video()
        return ref.video_key if ref else ""

    async def scroll_next(self) -> bool:
        """Swipe up on the document; True when a new video key appeared.

        Occasionally over-scrolls and bounces back first (SPEC §2.8).
        """
        self.last_nav_variant = None
        if self.rng.random() < self.params.over_scroll_probability:
            self.last_nav_variant = await scrolling.over_scroll_and_bounce(
                self.page, self.params, self.rng, key_fn=self._current_key
            )

        before = await self._current_key()
        await scrolling.swipe(self.page, self.params, self.rng, direction="up")
        after = await self._current_key()
        return bool(after) and after != before

    async def rewatch_previous(self) -> bool:
        """Scroll back one video to rewatch, then return (SPEC §6.4 navigation variety)."""
        variant = await scrolling.back_scroll_rewatch(
            self.page, self.params, self.rng, key_fn=self._current_key
        )
        if variant:
            self.last_nav_variant = variant
            return True
        return False

    # -- ads -----------------------------------------------------------------
    async def detect_ad(self) -> AdInfo | None:
        group = self.selectors.group("ad_indicators")
        rec = await resolve_first_visible(self.page, "ad_indicators", group.candidates, timeout_ms=1500)
        if rec.matched_index is not None:
            return AdInfo(matched_selector=rec.matched_selector)

        scope_text = await visible_text(self.page, "[data-e2e='feed-item']")
        patterns = list(group.compiled_text_patterns) + [p for p in self.extra_ad_patterns]
        hit = match_text_patterns(scope_text, tuple(patterns)) if patterns else None
        if hit:
            return AdInfo(matched_text=hit)
        return None

    async def skip_ad(self, ad: AdInfo) -> str:
        """Ladder (R11): close button → text-pattern scroll-past → min-watch fallback."""
        group = self.selectors.group("ad_close_button")
        rec = await resolve_first_visible(self.page, "ad_close_button", group.candidates, timeout_ms=2000)
        if rec.matched_index is not None:
            el = self.page.locator(group.candidates[rec.matched_index]).first
            try:
                await self._tap(el)
                return "close_button"
            except Exception as exc:
                logger.debug("ad close tap failed (%s); falling back", exc)

        if ad.matched_text is not None:
            return "text_pattern"

        await asyncio.sleep(self.rng.uniform(2.0, 4.0))
        return "min_watch"

    # -- interactions ----------------------------------------------------------
    async def _tap(self, element) -> None:
        """Tap with the configured jitter offset (SPEC §6.4 tap positions)."""
        from ..browser import human_input

        await human_input.tap_with_jitter(
            self.page, element, max_offset_px=self.params.tap_offset_px, rng=self.rng
        )

    async def _tap_group(self, group_name: str) -> bool:
        group = self.selectors.group(group_name)
        rec = await resolve_first_visible(self.page, group_name, group.candidates)
        self.health.record(rec.group, rec.matched_index, rec.matched_selector)
        if rec.matched_index is None:
            return False
        el = self.page.locator(group.candidates[rec.matched_index]).first
        try:
            await self._tap(el)
            return True
        except Exception as exc:
            logger.debug("%s tap failed: %s", group_name, exc)
            return False

    async def like(self) -> bool:
        # R15: check liked-state first so we don't UNLIKE an already-liked video.
        indicator_candidates = self._nested("like_button", "liked_state_indicator")
        if indicator_candidates:
            rec = await resolve_first_visible(self.page, "like_button_liked", indicator_candidates, timeout_ms=1200)
            if rec.matched_index is not None:
                return True  # already liked
        return await self._tap_group("like_button")

    async def save(self) -> bool:
        # TikTok's "save" is the bookmark action.
        return await self._tap_group("save_button")

    async def comment(self, text: str) -> bool:
        from ..browser import human_input

        if not await self._tap_group("comment_open"):
            return False
        await asyncio.sleep(self.rng.uniform(0.5, 1.5))

        group = self.selectors.group("comment_input")
        rec = await resolve_first_visible(self.page, "comment_input", group.candidates)
        if rec.matched_index is None:
            return False
        el = self.page.locator(group.candidates[rec.matched_index]).first
        try:
            await human_input.type_human(
                self.page, el, text,
                wpm_range=(self.params.typing_wpm_min, self.params.typing_wpm_max),
                pause_probability=self.params.typing_pause_probability,
                rng=self.rng,
            )
        except Exception as exc:
            logger.debug("comment typing failed: %s", exc)
            return False
        await asyncio.sleep(self.rng.uniform(0.4, 1.2))
        return await self._tap_group("comment_send")

    async def follow(self, author_handle: str) -> bool:
        """Follow the current video's author via their profile link."""
        try:
            loc = self.page.locator(f"a[href*='/{author_handle}/']").first
            if await loc.count() == 0:
                return False
            await loc.click(timeout=5000)
            await asyncio.sleep(self.rng.uniform(1.2, 2.5))
            follow_btn = self.page.locator("button:has-text('Follow'), [aria-label*='Follow' i]").first
            if await follow_btn.count() == 0:
                return False
            await self._tap(follow_btn)
            await self.page.go_back(timeout=15_000)
            await asyncio.sleep(self.rng.uniform(0.8, 1.6))
            return True
        except Exception as exc:
            logger.debug("follow(%s) failed: %s", author_handle, exc)
            return False

    # -- identity / health -----------------------------------------------------
    async def check_login_state(self) -> bool:
        url = (self.page.url or "").lower()
        login_group = self.selectors.group("login_indicator")
        for hint in login_group.url_contains:
            if hint.lower() in url:
                return False
        rec = await resolve_first_visible(
            self.page, "logged_in_indicator", self.selectors.group("logged_in_indicator").candidates, timeout_ms=2500
        )
        self.health.record(rec.group, rec.matched_index, rec.matched_selector)
        if rec.matched_index is not None:
            return True
        # No positive indicator AND no login-wall URL → assume logged in (TikTok shows a
        # persistent login modal / redirect when logged out).
        return "login" not in url

    async def scan_bot_signals(self) -> list[BotSignal]:
        text = await visible_text(self.page, "[data-e2e='feed-item']")
        page_text = await visible_text(self.page)  # modals sit outside the feed item
        signals: list[BotSignal] = []
        for name, group in self.selectors.groups.items():
            if not name.startswith("bot_signals."):
                continue
            sig_type = name.split(".", 1)[1]
            haystack = f"{text} {page_text}"
            hit = match_text_patterns(haystack, group.compiled_text_patterns)
            if hit:
                signals.append(BotSignal(signal_type=sig_type, matched_text=hit))
        return signals

    # -- helpers -----------------------------------------------------------------
    def _nested(self, parent_group: str, key: str) -> tuple[str, ...]:
        """Read a nested candidate list (e.g. like_button.liked_state_indicator)."""
        group = self.selectors.groups.get(parent_group)
        if group is None:
            return ()
        sub = group.extras.get(key)
        return sub.candidates if sub else ()
