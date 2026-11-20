"""Shared Playwright DOM helpers for browser adapters (SPEC §7.2 resolution rules).

Adapters reference *logical groups* only; this module turns a group's ordered candidate
list into "first visible match wins" with the matched index recorded for drift detection
(SPEC §7.2 rule 1: primary failing + fallback matching repeatedly = stale selectors).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("warmer")


@dataclass
class MatchRecord:
    """Which candidate resolved a group (health stats → event_logs category='selector')."""

    group: str
    matched_index: int | None      # None = no candidate matched
    matched_selector: str | None = None


@dataclass
class SelectorHealth:
    """Per-session tally of candidate matches, for drift alarms (SPEC §7.2 rule 2)."""

    records: list[MatchRecord] = field(default_factory=list)

    def record(self, group: str, matched_index: int | None, selector: str | None = None) -> None:
        self.records.append(MatchRecord(group=group, matched_index=matched_index, matched_selector=selector))

    def drift_warnings(self, *, threshold: int = 5, window: int = 10) -> list[str]:
        """Groups whose PRIMARY candidate (index 0) failed in >= ``threshold`` of the last
        ``window`` resolutions while a fallback succeeded → 'selectors may be stale'."""
        warnings: list[str] = []
        by_group: dict[str, list[MatchRecord]] = {}
        for rec in self.records:
            by_group.setdefault(rec.group, []).append(rec)
        for group, recs in by_group.items():
            recent = recs[-window:]
            if len(recent) < threshold:
                continue
            fallback_hits = sum(1 for r in recent if r.matched_index is not None and r.matched_index > 0)
            if fallback_hits >= threshold:
                warnings.append(f"{group}: primary candidate failing ({fallback_hits}/{len(recent)} on fallback)")
        return warnings


async def resolve_first_visible(page, group_name: str, candidates: tuple[str, ...], *, timeout_ms: int = 3000) -> MatchRecord:
    """Try each candidate in order; return the first that resolves to a visible element.

    Short per-candidate timeout (SPEC §7.2 rule 1). A candidate "resolves" when
    ``locator.count() > 0`` and its first match is attached + visible within the budget —
    we do NOT wait the full timeout for every miss: count() is cheap, visibility gets a
    short grace so mid-animation elements still qualify.
    """
    from playwright.async_api import TimeoutError as PWTimeout

    per_candidate = max(300, timeout_ms // max(len(candidates), 1))
    for idx, candidate in enumerate(candidates):
        try:
            loc = page.locator(candidate).first
            if await loc.count() == 0:
                continue
            try:
                await loc.wait_for(state="visible", timeout=per_candidate)
            except PWTimeout:
                # Present but not yet visible — accept it (feed items animate in).
                pass
            return MatchRecord(group=group_name, matched_index=idx, matched_selector=candidate)
        except Exception as exc:  # malformed candidate shouldn't kill the session
            logger.debug("selector %r (%s) failed: %s", group_name, candidate, exc)
    return MatchRecord(group=group_name, matched_index=None)


async def visible_text(page, scope_selector: str | None = None) -> str:
    """Visible text of a scope (default: whole page body), collapsed whitespace.

    Used for ad/bot-signal text matching. Scoped to the current card when possible to cut
    false positives from nav chrome (SPEC §7.2 rule 3).
    """
    try:
        loc = page.locator(scope_selector).first if scope_selector else page.locator("body")
        text = await loc.inner_text(timeout=1500)
    except Exception:
        return ""
    return re.sub(r"\s+", " ", text or "").strip()


def match_text_patterns(text: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    """First pattern that matches (case-insensitive already compiled). Returns the raw source."""
    for pat in patterns:
        if pat.search(text):
            return pat.pattern
    return None
