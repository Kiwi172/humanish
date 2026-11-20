"""Persistent-context factory (SPEC §6.1).

``launch_persistent_context`` = one user-data-dir per account+platform:
``data/profiles/<account_id>_<platform>/`` — cookies, localStorage, IndexedDB and service
workers persist across sessions so the platform sees a *returning* device, not a fresh
profile (SPEC §2.2). Real Chrome channel first (real binary's TLS/HTTP2 fingerprint),
bundled Chromium as fallback; stealth applied via playwright-extra ``StealthPlugin`` when
installed, otherwise the in-repo init-script patches (R19).

Channel resolution order: explicit argument > ``PLAYWRIGHT_CHANNEL`` env
(``chrome|chromium|auto``) > auto-detect. Headless is read from ``HEADLESS`` env and made
explicit — never a silent default (SPEC §6.1 "Headless avoidance").
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def profile_dir_for(data_dir: str | Path, account_id: int, platform: str) -> str:
    """Stable Playwright user-data-dir path for one (account, platform)."""
    return f"{Path(data_dir).as_posix().rstrip('/')}/profiles/{account_id}_{platform}"


def resolve_channel(requested: str | None = None) -> tuple[str, bool]:
    """Resolve the browser channel to launch.

    Returns ``(channel, is_fallback)`` where ``is_fallback`` is True when "auto" had to
    settle on bundled Chromium because Google Chrome is not installed (worth logging).
    """
    want = (requested or os.environ.get("PLAYWRIGHT_CHANNEL", "auto")).strip().lower()
    if want in ("chrome", "chromium"):
        return want, False
    if want != "auto":
        raise ValueError(f"unknown PLAYWRIGHT_CHANNEL {want!r}; expected chrome|chromium|auto")

    for candidate in (
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        "/usr/bin/google-chrome",
        "/opt/google/chrome/google-chrome",
    ):
        if candidate and Path(candidate).exists():
            return "chrome", False
    # No system Chrome — fall back to the bundled Chromium Playwright ships.
    return "chromium", True


def headless_from_env() -> bool:
    """``HEADLESS`` env flag (default false = visible window, SPEC §6.1)."""
    raw = os.environ.get("HEADLESS", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ContextOptions:
    """Everything :func:`launch_persistent_context` needs, resolved by the caller.

    Keeping this a plain dataclass (instead of reading env inside launch) keeps the
    function trivially testable and lets services log exactly what was used.
    """

    user_data_dir: str
    channel: str = "chrome"
    headless: bool = False
    proxy: dict[str, str] | None = None          # from browser.proxy.parse_proxy (None = direct)
    user_agent: str | None = None                # fingerprint UA (mobile Chrome preset)
    timezone_id: str | None = None               # IANA — stable per account
    locale: str | None = None                    # BCP-47 — stable per account
    viewport: dict[str, int] | None = None       # {"width": .., "height": ..}
    device_scale_factor: float | None = None
    is_mobile: bool = True
    has_touch: bool = True
    extra_args: tuple[str, ...] = field(default_factory=tuple)


async def launch_persistent_context(options: ContextOptions):
    """Launch a Playwright persistent browser context for one account+platform.

    Returns ``(playwright, browser_context)`` — the caller owns both and must close them
    (``await context.close(); await playwright.stop()``). Stealth is applied here so no
    call site can forget it: plugin when available, init-script patches otherwise.
    """
    from playwright.async_api import async_playwright

    # Optional stealth layer (R19): prefer the maintained plugin when installed; the
    # in-repo init scripts are behaviorally equivalent and dependency-free.
    use_plugin = False
    try:
        from playwright_extra.plugin import StealthPlugin  # type: ignore[import-not-found]

        use_plugin = True
    except ImportError:
        pass

    pw_kwargs: dict = {}
    if use_plugin:
        pw_kwargs["plugins"] = [StealthPlugin()]

    async with async_playwright(**pw_kwargs) as playwright:  # type: ignore[arg-type]
        kwargs: dict = {
            "headless": options.headless,
            "channel": options.channel,
            "viewport": options.viewport or {"width": 412, "height": 915},
            "is_mobile": options.is_mobile,
            "has_touch": options.has_touch,
        }
        if options.device_scale_factor is not None:
            kwargs["device_scale_factor"] = options.device_scale_factor
        if options.user_agent:
            kwargs["user_agent"] = options.user_agent
        if options.timezone_id:
            kwargs["timezone_id"] = options.timezone_id
        if options.locale:
            kwargs["locale"] = options.locale
        if options.proxy is not None:
            kwargs["proxy"] = dict(options.proxy)
        if options.extra_args:
            kwargs["args"] = list(options.extra_args)

        Path(options.user_data_dir).mkdir(parents=True, exist_ok=True)
        context = await playwright.chromium.launch_persistent_context(
            options.user_data_dir, **kwargs
        )

        if not use_plugin:
            from .stealth_patches import build_init_scripts

            for script in build_init_scripts():
                await context.add_init_script(script)

        return playwright, context
