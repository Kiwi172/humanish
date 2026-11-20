"""Aggregates for CLI `stats` and the dashboard (Step 4): counters vs caps, warnings, selector health.

The query primitives live in :mod:`~account_warmer.core.repositories`; this service adds
presentation-level shaping: effective caps resolution (per-account override → global default),
ramp context, and day-bucketed series for charts.
"""
from __future__ import annotations


def resolve_effective_caps(account, warmer_cfg) -> dict[str, int]:
    """Per-account cap overrides with fallback to ``caps.daily_default`` (SPEC §2.12)."""
    defaults = warmer_cfg.caps.daily_default.model_dump()
    return {
        "videos": account.daily_video_cap if account.daily_video_cap is not None else defaults["videos"],
        "likes": account.daily_like_cap if account.daily_like_cap is not None else defaults["likes"],
        "comments": account.daily_comment_cap if account.daily_comment_cap is not None else defaults["comments"],
        "saves": account.daily_save_cap if account.daily_save_cap is not None else defaults["saves"],
        "follows": account.daily_follow_cap if account.daily_follow_cap is not None else defaults["follows"],
    }


def browser_mode() -> dict[str, object]:
    """The browser mode a session would launch in, for the dashboard (SPEC §6.1).

    "Headless avoidance" means the operator must always be able to see which mode is
    active and whether we fell back to bundled Chromium instead of real Chrome.
    """
    from ..browser.context import headless_from_env, resolve_channel

    channel, is_fallback = resolve_channel()
    return {
        "channel": channel,
        "channel_fallback": is_fallback,
        "headless": headless_from_env(),
    }


def compute_hints(db, accounts, sessions=None) -> list[dict[str, str]]:
    """Operator-facing risk hints for the dashboard (SPEC §6.1/§6.2, R1/R5/R17/R21).

    Deliberately limited to what can be verified locally in v1 — we cannot geo-lookup an
    exit IP's region without an external API, so tz↔proxy coherence (R5) is reported as a
    reminder listing each account's tz + proxy host rather than an automated verdict.
    """
    from ..core.repositories import selector_drift_warnings
    from ..utils.timeutil import parse_iso, utcnow

    hints: list[dict[str, str]] = []

    def add(severity: str, message: str) -> None:
        hints.append({"severity": severity, "message": message})

    mode = browser_mode()
    if mode["headless"]:
        add("warning", "HEADLESS=true — headless runs are easier to detect; headed is recommended (SPEC §6.1)")
    if mode["channel_fallback"]:
        add("warning", "Google Chrome not found — falling back to bundled Chromium + stealth patches (R2)")

    # R1/R17: accounts with no proxy, or several accounts sharing one exit.
    no_proxy = [a for a in accounts if not (a.proxy_url or "").strip()]
    if no_proxy:
        ids = ", ".join(f"#{a.id}" for a in no_proxy)
        add("warning", f"{len(no_proxy)} account(s) have no per-account proxy ({ids}) — "
                       "they share this machine's IP, the #1 multi-account tell (R1)")

    by_host: dict[str, list[int]] = {}
    for a in accounts:
        if a.proxy_url:
            by_host.setdefault(_proxy_host(a.proxy_url), []).append(a.id)
    for host, ids in sorted(by_host.items()):
        if len(ids) > 1:
            add("warning", f"accounts {', '.join(f'#{i}' for i in ids)} share proxy exit "
                           f"{host} — IP-level throttling risk (R17)")

    # R5: tz ↔ proxy coherence reminder (no geo API in v1 — operator verifies the region).
    for a in accounts:
        if a.timezone_id and a.timezone_id != "UTC" and not a.proxy_url:
            add("info", f"account #{a.id} tz={a.timezone_id} but no proxy set — "
                        "verify the exit IP's region matches (R5)")

    # R21: running sessions whose heartbeat went stale.
    for s in sessions or []:
        if s.status != "running":
            continue
        hb = parse_iso(getattr(s, "heartbeat_at", None))
        if hb is None:
            add("info", f"session #{s.id} (account #{s.account_id}) has no heartbeat yet")
        elif (utcnow() - hb).total_seconds() > 120:
            add("error", f"session #{s.id} (account #{s.account_id}) stalled — "
                         f"no heartbeat for {int((utcnow() - hb).total_seconds())}s (R21: check for machine sleep)")

    # R13/R14: selector drift per account.
    for a in accounts:
        for warning in selector_drift_warnings(db, account_id=a.id):
            add("warning", f"account #{a.id} selector drift — {warning} (update selectors/)")

    return hints


def _proxy_host(proxy_url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(proxy_url)
    if parts.hostname:
        return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
    return proxy_url

