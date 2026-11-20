"""Config-driven feed scrolling shared by browser adapters (SPEC §2.8, §6.4).

Both browser adapters need the same gesture machinery; keeping it here means the
``behavior.scroll`` / platform ``scroll_gesture`` parameters are interpreted in exactly
one place, and adapters stay thin. All randomness comes from the injected ``rng`` and the
injected :class:`~account_warmer.adapters.base.GestureParams` — no config reads here.
"""
from __future__ import annotations

import asyncio
import random

from .base import GestureParams


def _swipe_duration_ms(params: GestureParams, rng: random.Random) -> int:
    return rng.randint(params.swipe_duration_min_ms, params.swipe_duration_max_ms)


def _hold_ms(params: GestureParams, rng: random.Random) -> int:
    return rng.randint(params.hold_at_edge_min_ms, params.hold_at_edge_max_ms)


def _step_distance_px(params: GestureParams, rng: random.Random, viewport_h: int) -> int:
    """Per-step travel: from the configured px range, clamped to the viewport."""
    lo = min(params.step_height_min_px, params.step_height_max_px)
    hi = min(params.step_height_max_px, max(lo + 1, int(viewport_h * 0.95)))
    return rng.randint(lo, hi)


async def swipe(
    page,
    params: GestureParams,
    rng: random.Random,
    *,
    direction: str = "up",
    steps: int | None = None,
) -> None:
    """Perform one (or several incremental) human swipes on the feed."""
    from ..browser import human_input

    vp = page.viewport_size or {"width": 412, "height": 915}
    n_steps = max(1, params.steps if steps is None else steps)
    for i in range(n_steps):
        await human_input.swipe_to_next(
            page,
            start_y_frac=0.78 if direction == "up" else 0.22,
            distance_px=_step_distance_px(params, rng, vp["height"]),
            duration_ms=_swipe_duration_ms(params, rng),
            hold_at_edge_ms=_hold_ms(params, rng),
            jitter_pct=params.jitter_pct,
            direction=direction,
            rng=rng,
        )
        if i < n_steps - 1:
            await asyncio.sleep(rng.uniform(0.15, 0.6))
    await asyncio.sleep(rng.uniform(0.4, 1.2))


async def over_scroll_and_bounce(
    page,
    params: GestureParams,
    rng: random.Random,
    *,
    key_fn,
) -> str | None:
    """Occasionally overshoot then bounce back (SPEC §2.8 "over-scroll-and-bounce-back").

    Returns a short label describing what happened ("overshoot_bounce") when the bounce
    restored the original video, else None. Best-effort: any failure is swallowed since
    this is cosmetic realism, not correctness.
    """
    before = await key_fn()
    await swipe(page, params, rng, direction="up", steps=1)
    await asyncio.sleep(rng.uniform(0.2, 0.9))
    await swipe(page, params, rng, direction="down", steps=1)
    await asyncio.sleep(rng.uniform(0.3, 1.0))
    after = await key_fn()
    return "overshoot_bounce" if after and after == before else None


async def back_scroll_rewatch(
    page,
    params: GestureParams,
    rng: random.Random,
    *,
    key_fn,
) -> str | None:
    """Scroll back one video, dwell, and come forward again (SPEC §6.4 rewatch).

    Returns "rewatch" when the previous video actually came back on screen.
    """
    before = await key_fn()
    await swipe(page, params, rng, direction="down", steps=1)
    await asyncio.sleep(rng.uniform(1.5, 6.0))  # a real rewatch lingers a moment
    after = await key_fn()
    if after and after != before:
        # We landed on the previous video — return forward to where we were.
        await swipe(page, params, rng, direction="up", steps=1)
        return "rewatch"
    return None
