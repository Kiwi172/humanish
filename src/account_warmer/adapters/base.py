"""``PlatformAdapter`` ABC + shared dataclasses (SPEC §3).

Dependency rule (SPEC §4): adapters know *nothing* about the DB or config files except
through injected selector objects. A future mobile/native adapter implements this same
interface and nothing else changes in services/scheduler/dashboard.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class VideoRef:
    """Stable reference to the video currently on screen."""

    video_key: str                      # reel shortcode / aweme id (from URL via video_key_source)
    author_handle: str | None = None
    caption_snippet: str | None = None  # first ~200 chars, context only


@dataclass(frozen=True)
class AdInfo:
    """Result of :meth:`PlatformAdapter.detect_ad` — how the ad was recognized."""

    matched_selector: str | None = None   # which candidate selector hit (None if text-only match)
    matched_text: str | None = None       # which text pattern hit


@dataclass(frozen=True)
class BotSignal:
    """One bot-detection indicator found by :meth:`PlatformAdapter.scan_bot_signals`."""

    signal_type: str            # key into selectors bot_signals (captcha / check_phone_email / rate_limit …)
    matched_text: str | None = None


@dataclass(frozen=True)
class GestureParams:
    """Gesture/typing realism parameters, injected by the service (SPEC §6.4, §10).

    Adapters must not read ``config/*.yaml`` themselves (SPEC §4 dependency rule), so the
    service resolves ``behavior.scroll`` + ``behavior.typing`` + the platform's
    ``scroll_gesture`` into this plain object and hands it over. Defaults mirror
    ``config/warmer.yaml`` so an adapter built without params still behaves sanely.
    """

    steps: int = 1                       # incremental scrolls per "next video" advance
    step_height_min_px: int = 280
    step_height_max_px: int = 640
    jitter_pct: float = 15.0             # offset on tap position / scroll increments
    over_scroll_probability: float = 0.03
    back_scroll_probability: float = 0.02
    swipe_duration_min_ms: int = 160
    swipe_duration_max_ms: int = 420
    hold_at_edge_min_ms: int = 50
    hold_at_edge_max_ms: int = 160
    typing_wpm_min: float = 90.0
    typing_wpm_max: float = 160.0
    typing_pause_probability: float = 0.35
    tap_offset_px: float = 8.0           # max distance from element center when tapping

    def __post_init__(self) -> None:
        if self.step_height_max_px < self.step_height_min_px:
            raise ValueError("step_height_max_px must be >= step_height_min_px")
        if self.swipe_duration_max_ms < self.swipe_duration_min_ms:
            raise ValueError("swipe_duration_max_ms must be >= swipe_duration_min_ms")
        if self.typing_wpm_max < self.typing_wpm_min:
            raise ValueError("typing_wpm_max must be >= typing_wpm_min")


class PlatformAdapter(abc.ABC):
    """Per-session API shared by all platform implementations.

    ``page`` is the Playwright Page bound to the account's persistent context;
    ``selectors`` is a loaded :class:`~account_warmer.adapters.selector_loader.PlatformSelectors`.
    All methods are awaited in async contexts (Playwright python is asyncio-based).
    """

    def __init__(self, page, selectors, *, params: GestureParams | None = None):
        self.page = page
        self.selectors = selectors
        self.params = params or GestureParams()

    # -- feed navigation ----------------------------------------------------
    @abc.abstractmethod
    async def open_feed(self, mode: str) -> None:
        """Navigate to the requested feed (e.g. "reels"/"home" for IG, "fyp" for TikTok)."""

    @abc.abstractmethod
    async def current_video(self) -> VideoRef | None:
        """Describe the video currently on screen (key from URL via selectors.video_key_source)."""

    @abc.abstractmethod
    async def scroll_next(self) -> bool:
        """Advance to the next video with human-like scrolling. False if the feed ended/stalled.

        May itself perform an over-scroll + bounce-back (SPEC §2.8) when the configured
        ``over_scroll_probability`` fires.
        """

    @abc.abstractmethod
    async def rewatch_previous(self) -> bool:
        """Scroll back one video to rewatch (SPEC §6.4 "feed navigation variety")."""

    # -- ads -----------------------------------------------------------------
    @abc.abstractmethod
    async def detect_ad(self) -> AdInfo | None:
        """Detect whether the current card is an ad (selectors + text patterns, OR logic)."""

    @abc.abstractmethod
    async def skip_ad(self, ad: AdInfo) -> str:
        """Skip method ladder: close button → min-watch fallback. Returns 'close_button' | 'min_watch'."""

    # -- interactions ----------------------------------------------------------
    @abc.abstractmethod
    async def like(self) -> bool: ...

    @abc.abstractmethod
    async def save(self) -> bool: ...

    @abc.abstractmethod
    async def comment(self, text: str) -> bool: ...

    @abc.abstractmethod
    async def follow(self, author_handle: str) -> bool: ...

    # -- identity / health -----------------------------------------------------
    @abc.abstractmethod
    async def check_login_state(self) -> bool:
        """True if logged_in_indicator resolves (or url not in login_indicator.url_contains)."""

    @abc.abstractmethod
    async def scan_bot_signals(self) -> list[BotSignal]:
        """Scan visible card/overlay text for captcha / phone-check / rate-limit indicators."""

    # -- lifecycle ---------------------------------------------------------------
    def close(self) -> None:  # pragma: no cover - default no-op
        pass
