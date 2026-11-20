"""Manual login flow (SPEC §2.3 / R8).

Opens a *visible* browser on the account's home page using that account's real profile dir,
fingerprint and proxy; waits up to ``timeout`` seconds for ``logged_in_indicator`` selectors
to confirm state; then persists session state (cookies etc. live in the user-data-dir) and
marks the account ``active`` (+ ``last_login_at``). Timeout / failed verify → ``needs_login``.

First login is always human-operated (password/2FA/QR); do it from the same proxy/IP you'll
warm with (SPEC R8).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("warmer")


@dataclass(frozen=True)
class LoginResult:
    account_id: int
    success: bool
    reason: str  # "verified" | "timeout" | "error:<msg>"


def _adapter_cls(platform: str):
    from ..adapters.instagram import InstagramAdapter
    from ..adapters.tiktok import TikTokAdapter

    return InstagramAdapter if platform == "instagram" else TikTokAdapter


def _build_context_options(account, root_path, *, headless: bool | None = None):
    """Resolve everything launch_persistent_context needs for one account."""
    from ..browser.context import ContextOptions, headless_from_env, profile_dir_for, resolve_channel
    from ..browser.proxy import parse_proxy
    from ..core.config import data_dir_from_env
    from ..core.fingerprint import build_fingerprint

    channel, is_fallback = resolve_channel()
    if is_fallback:
        logger.warning("PLAYWRIGHT_CHANNEL=auto fell back to bundled Chromium (no system Chrome found)")

    fp = build_fingerprint(account, channel=channel)
    proxy_url = account.proxy_url or os.environ.get("DEFAULT_PROXY") or None
    return ContextOptions(
        user_data_dir=profile_dir_for(str(data_dir_from_env(root_path)), account.id, account.platform),
        channel=channel,
        headless=headless if headless is not None else headless_from_env(),
        proxy=parse_proxy(proxy_url) if proxy_url else None,
        user_agent=fp.user_agent,
        timezone_id=fp.timezone_id,
        locale=fp.locale,
        viewport=fp.viewport,
        device_scale_factor=fp.device_scale_factor,
        is_mobile=fp.is_mobile,
        has_touch=fp.has_touch,
    )


def _base_url_for(platform: str) -> str:
    from ..adapters.selector_loader import load_platform_selectors

    selectors = load_platform_selectors(platform)
    if platform == "instagram":
        return selectors.base_urls.get("home", "https://www.instagram.com/")
    return selectors.base_urls.get("fyp", "https://www.tiktok.com/")


async def run_manual_login(
    *,
    account_id: int,
    timeout_s: float = 900.0,
    root: str | None = None,
    headless: bool | None = None,
) -> LoginResult:
    """Open the visible browser and wait for a human to log in; verify + persist state."""
    from ..adapters.selector_loader import load_platform_selectors
    from ..browser.context import launch_persistent_context
    from ..core import repositories as repo
    from ..core.config import db_path_from_env, find_project_root, load_env
    from ..core.db import make_engine, new_session_factory, session_scope

    root_path = find_project_root(root)
    load_env(root_path)
    engine = make_engine(db_path_from_env(root_path))
    factory = new_session_factory(engine)

    with session_scope(factory) as db:
        account = repo.get_account(db, account_id)
        if account is None:
            raise LookupError(f"account {account_id} not found")
        options = _build_context_options(account, root_path, headless=headless)
        platform = account.platform

    adapter_cls = _adapter_cls(platform)
    selectors = load_platform_selectors(platform)

    logger.info("login: opening browser for %s (timeout %.0fs)", platform, timeout_s)
    playwright, context = await launch_persistent_context(options)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        adapter = adapter_cls(page, selectors)

        # Land on the home page — logged-out users see the login wall there.
        try:
            await page.goto(_base_url_for(platform), wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.warning("login: initial goto failed (%s); waiting for manual navigation", exc)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while True:
            if await adapter.check_login_state():
                with session_scope(factory) as db:
                    acct = repo.get_account(db, account_id)
                    repo.update_account(acct, status="active", last_login_at=repo.utcnow_iso())
                    repo.log_event(
                        db, level="info", category="login",
                        message=f"manual login verified for account {account_id}",
                        account_id=account_id,
                    )
                logger.info("login: account %s is active", account_id)
                return LoginResult(account_id=account_id, success=True, reason="verified")

            if loop.time() >= deadline:
                with session_scope(factory) as db:
                    acct = repo.get_account(db, account_id)
                    repo.update_account(acct, status="needs_login")
                    repo.log_event(
                        db, level="warning", category="login",
                        message=f"manual login timed out after {timeout_s:.0f}s (account {account_id})",
                        account_id=account_id,
                    )
                return LoginResult(account_id=account_id, success=False, reason="timeout")

            await asyncio.sleep(2.0)
    finally:
        try:
            await context.close()
        except Exception:  # pragma: no cover - best effort teardown
            pass
        await playwright.stop()


async def probe_login_state(*, account_id: int, root: str | None = None) -> bool:
    """Quick session-state check (backs `warmer check-login`).

    Opens the profile in a short-lived headless context and checks the logged-in indicator
    once — no waiting for a human. Returns True when the session still looks valid.
    """
    from ..adapters.selector_loader import load_platform_selectors
    from ..browser.context import launch_persistent_context
    from ..core import repositories as repo
    from ..core.config import db_path_from_env, find_project_root, load_env
    from ..core.db import make_engine, new_session_factory, session_scope

    root_path = find_project_root(root)
    load_env(root_path)
    engine = make_engine(db_path_from_env(root_path))
    factory = new_session_factory(engine)

    with session_scope(factory) as db:
        account = repo.get_account(db, account_id)
        if account is None:
            raise LookupError(f"account {account_id} not found")
        options = _build_context_options(account, root_path, headless=True)  # probe can be backgrounded
        platform = account.platform

    adapter_cls = _adapter_cls(platform)
    selectors = load_platform_selectors(platform)

    playwright, context = await launch_persistent_context(options)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        adapter = adapter_cls(page, selectors)
        return bool(await adapter.check_login_state())
    finally:
        try:
            await context.close()
        except Exception:  # pragma: no cover - best effort teardown
            pass
        await playwright.stop()
