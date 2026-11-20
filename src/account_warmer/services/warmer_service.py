"""Warms one session end-to-end (SPEC §3 pseudocode). **Step 3.**

The only place sessions are created/closed: open persistent context → build behavior plan
(scheduler) → loop steps (watch/dwell/decide/idle, dedupe checks via repositories) → record
to DB → teardown with final counters + stop_reason. Translates adapter results into DB
writes and ``event_logs`` rows; enforces ``warnings.pause_after`` auto-flagging.

Error containment per SPEC R20: a failed scroll is retried once, then logged + continued;
the session aborts when the tolerated-error budget (``session.max_errors_before_abort``)
is exhausted.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time as _time
from dataclasses import dataclass, field

logger = logging.getLogger("warmer")


@dataclass
class SessionCounters:
    """In-memory session totals; flushed to the sessions row at teardown."""

    videos_watched: int = 0
    ads_skipped: int = 0
    likes_given: int = 0
    comments_made: int = 0
    saves_done: int = 0
    follows_done: int = 0
    warnings_count: int = 0
    errors_count: int = 0


@dataclass(frozen=True)
class SessionOutcome:
    account_id: int
    session_id: int | None
    status: str          # completed | aborted | error
    stop_reason: str | None
    counters: SessionCounters = field(default_factory=SessionCounters)


def _adapter_cls(platform: str):
    """Return (adapter class, extra-ad-pattern compiler) for a platform."""
    from ..adapters.instagram import InstagramAdapter, compile_extra_patterns as ig_compile
    from ..adapters.tiktok import TikTokAdapter

    return (InstagramAdapter if platform == "instagram" else TikTokAdapter), ig_compile


async def run_warming_session(
    *,
    account_ids: list[int],
    now: bool = False,
    with_dashboard: bool = False,  # Step 4: uvicorn in the same loop
    root: str | None = None,
    headless: bool | None = None,
) -> list[SessionOutcome]:
    """Run warming for one or more accounts (sequential — one browser profile at a time).

    ``now=True`` runs each account's session immediately; otherwise the scheduler decides
    start times (supervisor mode keeps looping until stopped — Step 4 refines that loop).
    """
    from ..core.settings_overrides import load_config_with_overrides

    cfg = load_config_with_overrides(root)
    outcomes: list[SessionOutcome] = []
    for account_id in account_ids:
        outcome = await _run_one_session(
            account_id=account_id, cfg=cfg, root=root, headless=headless, now=now
        )
        outcomes.append(outcome)
    return outcomes


async def _run_one_session(
    *,
    account_id: int,
    cfg,
    root: str | None = None,
    headless: bool | None = None,
    now: bool = False,
) -> SessionOutcome:
    from ..adapters.selector_loader import load_platform_selectors
    from ..browser.context import launch_persistent_context
    from ..core import repositories as repo
    from ..core.behavior import (
        decide_action, load_comment_pool, maybe_idle, pick_comment,
        sample_dwell, sample_idle, set_global_behavior,
    )
    from ..core.config import db_path_from_env, find_project_root, load_env, load_platform_config
    from ..core.db import make_engine, new_session_factory, session_scope
    from ..core.scheduler import plan_account_day

    root_path = find_project_root(root)
    load_env(root_path)
    engine = make_engine(db_path_from_env(root_path))
    factory = new_session_factory(engine)
    set_global_behavior(cfg)  # save/follow probabilities for decide_action

    with session_scope(factory) as db:
        account = repo.get_account(db, account_id)
        if account is None:
            raise LookupError(f"account {account_id} not found")
        platform_cfg = load_platform_config(account.platform, root_path)
        selectors = load_platform_selectors(account.platform, root_path)
        comment_pool = load_comment_pool(account.platform, root_path)

        # One writer per account (SPEC §3): refuse to start if one is already running.
        if repo.get_running_session(db, account.id) is not None:
            logger.warning("session for account %s already running — skipping", account_id)
            return SessionOutcome(account_id=account_id, session_id=None, status="aborted", stop_reason="already_running")

        # Plan the session (duration + feed mode + video budget). `now` bypasses start-time waiting.
        from .stats_service import resolve_effective_caps

        day_caps = resolve_effective_caps(account, cfg)
        videos_today = repo.count_today(db, account, "video")
        day_plan = plan_account_day(
            cfg, platform_cfg, account,
            daily_video_cap=day_caps["videos"], videos_today=videos_today,
        )
        plan = day_plan.sessions[0] if day_plan and day_plan.sessions else None
        if plan is None:
            return SessionOutcome(account_id=account_id, session_id=None, status="aborted", stop_reason="no_plan")
        if plan.video_budget <= 0:
            # Daily video cap already spent (SPEC §2.12) — nothing to warm today.
            logger.info("account %s at daily video cap (%d/%d) — skipping session",
                        account_id, videos_today, day_caps["videos"])
            with session_scope(factory) as db:
                repo.log_event(
                    db, level="info", category="limit",
                    message=f"daily video cap reached ({videos_today}/{day_caps['videos']}) — session skipped",
                    account_id=account_id,
                )
            return SessionOutcome(account_id=account_id, session_id=None, status="aborted", stop_reason="daily_video_cap_reached")

        from .login_service import _build_context_options  # shared context resolution

        options = _build_context_options(account, root_path, headless=headless)

        session = repo.create_session(
            db,
            account_id=account.id,
            planned_duration_s=plan.duration_s,
            proxy_used=_redact(options.proxy),
            fingerprint_hash=_fp_hash(account),
        )
        repo.start_session(db, session)
        session_id = session.id

    adapter_cls, compile_patterns = _adapter_cls(account.platform)
    import os as _os

    rng = random.Random()  # per-session RNG; seedable in tests via env WARMER_SEED
    seed_env = _os.environ.get("WARMER_SEED")
    if seed_env:
        rng.seed(int(seed_env))

    counters = SessionCounters()
    stop_reason: str | None = "duration_reached"
    status = "completed"

    logger.info(
        "session %s started for account %s (%s, mode=%s, planned %.0fs)",
        session_id, account_id, account.platform, plan.feed_mode, plan.duration_s,
    )

    playwright, context = await launch_persistent_context(options)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        from ..core.behavior import build_gesture_params

        gesture_params = build_gesture_params(cfg, platform_cfg)
        adapter = adapter_cls(page, selectors, rng=rng, params=gesture_params)
        extra_patterns = compile_patterns(platform_cfg.extra_ad_text_patterns)
        if hasattr(adapter, "extra_ad_patterns"):
            adapter.extra_ad_patterns = extra_patterns

        # Pre-session login check (R9): don't blind-scroll on a login wall.
        if not await adapter.check_login_state():
            detail: dict = {}
            if cfg.warnings.screenshots_on_warning:
                shot_path = await _capture_screenshot(
                    page, root_path=root_path, account_id=account_id,
                    session_id=session_id, reason="login_wall",
                )
                if shot_path:
                    detail["screenshot"] = shot_path
            with session_scope(factory) as db:
                repo.update_account(repo.get_account(db, account_id), status="needs_login")
                repo.log_event(
                    db, level="warning", category="login",
                    message=f"session {session_id}: login wall detected before start (account {account_id})",
                    account_id=account_id, session_id=session_id,
                    detail=detail or None,
                )
            stop_reason = "login_wall"
            status = "aborted"
        else:
            await adapter.open_feed(plan.feed_mode)

        deadline = _time.monotonic() + plan.duration_s
        max_errors = cfg.session.max_errors_before_abort
        consecutive_scroll_fails = 0
        video_budget = plan.video_budget  # 0 = unbounded (no daily cap configured)
        last_login_check = _time.monotonic()
        login_check_interval_s = max(60.0, plan.duration_s / 3)

        while _time.monotonic() < deadline and status != "aborted":
            # 0a. Operator stop request from the dashboard (SPEC §2.17).
            with session_scope(factory) as db:
                stop_flag = repo.stop_requested(db, session_id)
            if stop_flag:
                logger.info("session %s: user stop requested", session_id)
                stop_reason = "user_stop"
                status = "aborted"
                break

            # 0b. Heartbeat so the dashboard can spot a stalled session (SPEC R21).
            with session_scope(factory) as db:
                repo.touch_heartbeat(db, session_id)

            # 0c. Mid-session login-wall re-check (SPEC R9: no blind scrolling on a login wall).
            if _time.monotonic() - last_login_check >= login_check_interval_s:
                last_login_check = _time.monotonic()
                if not await adapter.check_login_state():
                    counters.warnings_count += 1
                    detail_login: dict = {}
                    if cfg.warnings.screenshots_on_warning:
                        shot_path = await _capture_screenshot(
                            page, root_path=root_path, account_id=account_id,
                            session_id=session_id, reason="login_wall_mid",
                        )
                        if shot_path:
                            detail_login["screenshot"] = shot_path
                    with session_scope(factory) as db:
                        repo.update_account(repo.get_account(db, account_id), status="needs_login")
                        repo.log_event(
                            db, level="warning", category="login",
                            message=f"session {session_id}: login wall detected mid-session (account {account_id})",
                            account_id=account_id, session_id=session_id,
                            detail=detail_login or None,
                        )
                    stop_reason = "login_wall"
                    status = "aborted"
                    break

            # 0d. Daily video cap reached (SPEC §2.12) — stop watching, keep the session clean.
            if video_budget and counters.videos_watched >= video_budget:
                stop_reason = "video_budget_reached"
                status = "completed"
                break

            # 1. Bot-signal scan (SPEC §6.5).
            signals = await adapter.scan_bot_signals()
            if signals:
                for sig in signals:
                    counters.warnings_count += 1
                    detail: dict = {"signal": sig.signal_type, "matched_text": sig.matched_text}
                    # Screenshot on warning (SPEC §2.16, config warnings.screenshots_on_warning).
                    if cfg.warnings.screenshots_on_warning:
                        shot_path = await _capture_screenshot(
                            page, root_path=root_path, account_id=account_id,
                            session_id=session_id, reason=sig.signal_type,
                        )
                        if shot_path:
                            detail["screenshot"] = shot_path
                    with session_scope(factory) as db:
                        repo.log_event(
                            db, level="warning", category="bot_signal",
                            message=f"bot signal {sig.signal_type!r} (matched {sig.matched_text!r})",
                            account_id=account_id, session_id=session_id,
                            detail=detail,
                        )
                stop_reason = f"bot_signal:{signals[0].signal_type}"
                status = "aborted"
                # Auto-flag per warnings.pause_after policy.
                with session_scope(factory) as db:
                    recent = repo.warnings_in_window(
                        db, account_id, within_hours=float(cfg.warnings.pause_after.within_hours)
                    )
                    if recent >= cfg.warnings.pause_after.count and (
                        repo.get_account(db, account_id).status != "flagged"
                    ):
                        repo.update_account(repo.get_account(db, account_id), status="flagged")
                        repo.log_event(
                            db, level="warning", category="bot_signal",
                            message=f"account {account_id} auto-flagged after {recent} warnings in window",
                            account_id=account_id, session_id=session_id,
                        )
                break

            # 2. Ad detection + skip ladder (R11).
            ad = await adapter.detect_ad()
            if ad is not None:
                method = await adapter.skip_ad(ad)
                counters.ads_skipped += 1
                video_ref = await adapter.current_video()
                with session_scope(factory) as db:
                    repo.record_watched(
                        db, account_id=account.id,
                        video_key=(video_ref.video_key if video_ref else f"ad-{int(_time.monotonic())}"),
                        author_handle=video_ref.author_handle if video_ref else None,
                        watch_seconds=0.0, session_id=session_id,
                        was_ad=True, ad_skip_method=method,
                    )
                    repo.log_event(
                        db, level="info", category="ad",
                        message=f"ad skipped via {method}",
                        account_id=account_id, session_id=session_id,
                        detail={"matched_selector": ad.matched_selector, "matched_text": ad.matched_text},
                    )
                # After skipping, advance to the next video.
                if not await _safe_scroll(adapter, rng):
                    consecutive_scroll_fails += 1
                else:
                    consecutive_scroll_fails = 0
                continue

            # 3. Watch current video with sampled dwell (longer when we intend to interact).
            video_ref = await adapter.current_video()
            if video_ref is None or not video_ref.video_key:
                counters.errors_count += 1
                with session_scope(factory) as db:
                    repo.log_event(
                        db, level="warning", category="selector",
                        message=f"session {session_id}: no video key extractable from URL {page.url!r}",
                        account_id=account_id, session_id=session_id,
                    )
                if counters.errors_count >= max_errors:
                    stop_reason = "error_budget_exhausted"
                    status = "aborted"
                    break
                await asyncio.sleep(2.0)
                continue

            # Decide BEFORE dwelling so interact-intent can stretch the watch (SPEC §6.4).
            with session_scope(factory) as db:
                acct = repo.get_account(db, account.id)
                caps = _effective_caps(acct, cfg)
                counters_today = {
                    "likes": repo.count_today(db, acct, "like"),
                    "comments": repo.count_today(db, acct, "comment"),
                    "saves": repo.count_today(db, acct, "save"),
                    "follows": repo.count_today(db, acct, "follow"),
                }
            ramp_row = cfg.ramp_row(acct.ramp_profile, _account_age_days(acct))
            decision = decide_action(rng, ramp_row, counters_today, caps)

            dwell = sample_dwell(rng, cfg, interact_intent=decision is not None)
            await asyncio.sleep(dwell)
            counters.videos_watched += 1
            with session_scope(factory) as db:
                repo.record_watched(
                    db, account_id=account.id, video_key=video_ref.video_key,
                    author_handle=video_ref.author_handle, caption_snippet=video_ref.caption_snippet,
                    watch_seconds=dwell, session_id=session_id, was_ad=False,
                )

            # 4. Execute the decided interaction (dedupe checks + caps as backstop).
            if decision is not None:
                ok = await _execute_action(adapter, db_factory=factory, account=acct, video_ref=video_ref,
                                           decision=decision, session_id=session_id, comment_pool=comment_pool, rng=rng)
                if ok:
                    if decision == "like":
                        counters.likes_given += 1
                    elif decision == "save":
                        counters.saves_done += 1
                    elif decision == "comment":
                        counters.comments_made += 1
                    elif decision == "follow":
                        counters.follows_done += 1

            # 5. Advance to the next video (with retry + error containment, R20).
            if not await _safe_scroll(adapter, rng):
                consecutive_scroll_fails += 1
                if consecutive_scroll_fails >= max_errors:
                    stop_reason = "scroll_stall"
                    status = "aborted"
                    break
            else:
                consecutive_scroll_fails = 0

            # 5b. Occasionally scroll back to rewatch the previous video (SPEC §6.4).
            if rng.random() < gesture_params.back_scroll_probability:
                await _safe_rewatch(adapter, rng)

            # 6. Occasional idle burst ("put the phone down").
            if maybe_idle(rng, cfg):
                pause_s = sample_idle(rng, cfg)
                logger.debug("idle burst %.1fs", pause_s)
                await asyncio.sleep(pause_s)

        # Teardown: flush counters + close session row.
        with session_scope(factory) as db:
            from ..core.models import Session as _SessionModel

            sess = db.get(_SessionModel, session_id)
            repo.close_session(
                db, sess, status=status, stop_reason=stop_reason,
                actual_duration_s=int(_time.monotonic() - (deadline - plan.duration_s)),
            )
            sess.videos_watched = counters.videos_watched
            sess.ads_skipped = counters.ads_skipped
            sess.likes_given = counters.likes_given
            sess.comments_made = counters.comments_made
            sess.saves_done = counters.saves_done
            sess.follows_done = counters.follows_done
            sess.warnings_count = counters.warnings_count
            sess.errors_count = counters.errors_count
            db.flush()

        # Selector health: record per-group match results + check for drift (SPEC §7.2).
        with session_scope(factory) as db:
            matches = [
                {"group": r.group, "matched_index": r.matched_index}
                for r in adapter.health.records
            ]
            repo.record_selector_matches(
                db, account_id=account.id, session_id=session_id, matches=matches
            )
            # In-session drift warnings.
            for warning in adapter.health.drift_warnings():
                repo.log_event(
                    db, level="warning", category="selector",
                    message=f"selector drift: {warning}", account_id=account_id, session_id=session_id,
                )
            # Cross-session drift (last 10 sessions).
            for warning in repo.selector_drift_warnings(db, account_id=account.id):
                repo.log_event(
                    db, level="warning", category="selector",
                    message=f"cross-session selector drift: {warning}",
                    account_id=account_id, session_id=session_id,
                )

    except Exception as exc:  # noqa: BLE001 - session-level containment (R20)
        logger.exception("session %s crashed: %s", session_id, exc)
        status = "error"
        stop_reason = f"error:{type(exc).__name__}"
        counters.errors_count += 1
        # Screenshot on crash for post-mortem.
        detail: dict = {"exception": str(exc)}
        if cfg.warnings.screenshots_on_warning:
            try:
                shot_path = await _capture_screenshot(
                    page, root_path=root_path, account_id=account_id,
                    session_id=session_id, reason="crash",
                )
                if shot_path:
                    detail["screenshot"] = shot_path
            except Exception:  # pragma: no cover - page may be dead
                pass
        with session_scope(factory) as db:
            from ..core.models import Session as _SessionModel

            sess = db.get(_SessionModel, session_id)
            if sess is not None and sess.status == "running":
                repo.close_session(db, sess, status="error", stop_reason=stop_reason)
            repo.log_event(
                db, level="error", category="app",
                message=f"session {session_id} crashed: {type(exc).__name__}: {exc}",
                account_id=account_id, session_id=session_id,
                detail=detail,
            )
    finally:
        try:
            await context.close()
        except Exception:  # pragma: no cover - best effort teardown
            pass
        await playwright.stop()

    logger.info(
        "session %s finished (%s, reason=%s): videos=%d ads=%d likes=%d comments=%d saves=%d follows=%d",
        session_id, status, stop_reason, counters.videos_watched, counters.ads_skipped,
        counters.likes_given, counters.comments_made, counters.saves_done, counters.follows_done,
    )
    return SessionOutcome(account_id=account_id, session_id=session_id, status=status,
                          stop_reason=stop_reason, counters=counters)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _capture_screenshot(page, *, root_path, account_id: int, session_id: int, reason: str) -> str | None:
    """Save a screenshot to logs/screenshots/ and return the relative path (or None on failure)."""
    from pathlib import Path

    try:
        screenshots_dir = Path(root_path) / "logs" / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = _time.strftime("%Y%m%d_%H%M%S")
        filename = f"acct{account_id}_sess{session_id}_{reason}_{ts}.png"
        filepath = screenshots_dir / filename
        await page.screenshot(path=str(filepath), full_page=False)
        logger.debug("screenshot saved: %s", filepath)
        return str(filepath.relative_to(root_path))
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("screenshot failed: %s", exc)
        return None


async def _safe_scroll(adapter, rng: random.Random) -> bool:
    """Scroll with one retry (config session.scroll_retry_on_fail); log + continue on fail."""
    try:
        if await adapter.scroll_next():
            return True
        # Same video twice in a row — retry once after a short wait.
        await asyncio.sleep(rng.uniform(1.0, 2.5))
        return bool(await adapter.scroll_next())
    except Exception as exc:
        logger.debug("scroll failed: %s", exc)
        return False


async def _safe_rewatch(adapter, rng: random.Random) -> bool:
    """Best-effort back-scroll to rewatch (SPEC §6.4) — never breaks the session."""
    try:
        return bool(await adapter.rewatch_previous())
    except Exception as exc:
        logger.debug("rewatch scroll failed: %s", exc)
        return False


async def _execute_action(adapter, *, db_factory, account, video_ref, decision: str, session_id: int, comment_pool, rng) -> bool:
    """Run one interaction through the adapter + DB (dedupe backstop via repositories)."""
    from ..core import repositories as repo
    from ..core.behavior import pick_comment
    from ..core.db import session_scope

    with session_scope(db_factory) as db:
        if decision == "like" and repo.already_liked(db, account.id, video_ref.video_key):
            return False
        if decision == "save" and repo.already_saved(db, account.id, video_ref.video_key):
            return False
        if decision == "comment" and repo.already_commented(db, account.id, video_ref.video_key):
            return False
        if decision == "follow":
            target = video_ref.author_handle
            if not target or repo.already_followed(db, account.id, target):
                return False

    try:
        if decision == "like":
            ok = await adapter.like()
        elif decision == "save":
            ok = await adapter.save()
        elif decision == "comment":
            # Topic-tagged pools per account (SPEC §2.10): pick from the account's
            # configured tags, falling back to the general pool when none are set.
            tags = repo.account_comment_tags(account)
            text, tag = pick_comment(rng, comment_pool, tags=tags or None)
            ok = await adapter.comment(text)
        else:  # follow
            ok = await adapter.follow(video_ref.author_handle or "")
    except Exception as exc:
        logger.debug("action %s failed: %s", decision, exc)
        return False

    if not ok:
        return False

    with session_scope(db_factory) as db:
        if decision == "comment":
            row = repo.add_comment(
                db, account_id=account.id, video_key=video_ref.video_key,
                comment_text=text, author_handle=video_ref.author_handle,
                template_key=tag, session_id=session_id,
            )
            return row is not None
        if decision == "follow":
            row = repo.add_action(
                db, account_id=account.id, action_type="follow",
                target_handle=video_ref.author_handle, session_id=session_id,
            )
            return row is not None
        row = repo.add_action(
            db, account_id=account.id, action_type=decision,
            video_key=video_ref.video_key, session_id=session_id,
        )
        return row is not None


def _effective_caps(account, cfg) -> dict[str, int]:
    from .stats_service import resolve_effective_caps

    caps = resolve_effective_caps(account, cfg)
    # decide_action expects keys: likes/comments/saves/follows (no "videos").
    return {k: v for k, v in caps.items() if k != "videos"}


def _account_age_days(account) -> int:
    from ..utils.timeutil import parse_iso, utcnow

    created = parse_iso(getattr(account, "created_at", None)) or utcnow()
    return max(0, int((utcnow() - created).total_seconds() // 86400))


def _fp_hash(account) -> str:
    from ..core.fingerprint import build_fingerprint

    return build_fingerprint(account).fingerprint_hash


# Small shim so warmer_service can redact proxies without importing browser.proxy twice.
def _redact(proxy_dict: dict[str, str] | None) -> str | None:
    if not proxy_dict:
        return None
    from ..browser.proxy import redact_proxy

    scheme = "socks5" if "socks" in proxy_dict.get("server", "") else "http"
    user = f"{proxy_dict['username']}:{proxy_dict.get('password', '')}@" if proxy_dict.get("username") else ""
    return redact_proxy(f"{scheme}://{user}{proxy_dict['server']}")
