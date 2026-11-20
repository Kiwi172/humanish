"""Human-like input primitives (SPEC §6.4).

Multi-step pointer/touch movement with jitter & offset (never the exact element center),
swipe gestures whose duration/speed come from config ``scroll_gesture``, and typing at a
sampled WPM with occasional mid-typing pauses. All timing params are injected — this
module owns *how* to move; the behavior planner owns *when/how much*.

Gestures are dispatched as real touch events (``touchstart/touchmove/touchend``) so mobile
feed pages that only listen for touch still react, with a mouse fallback where harmless.
"""
from __future__ import annotations

import asyncio
import random


def _jitter(rng: random.Random, pct: float) -> float:
    """Multiplicative jitter in [1 - pct/2, 1 + pct/2]."""
    return 1.0 + rng.uniform(-pct / 2.0, pct / 2.0)


async def swipe_to_next(
    page,
    *,
    start_y_frac: float = 0.75,
    distance_px: int,
    duration_ms: int,
    hold_at_edge_ms: int = 100,
    jitter_pct: float = 15.0,
    direction: str = "up",
    rng: random.Random | None = None,
) -> None:
    """Mobile-style swipe (touchstart → stepped touchmove with jitter → touchend).

    ``direction="up"`` advances the feed; ``"down"`` scrolls back to the previous video
    (SPEC §6.4 rewatch variety). ``distance_px`` is the total travel; it is split into
    ~8-14 micro-steps so the motion curve looks like a finger, not a teleport. The start
    point is offset horizontally from center by up to ±20% of the viewport width (people
    don't swipe on the exact middle).
    """
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down' (got {direction!r})")
    rng = rng or random.Random()
    vp = page.viewport_size or {"width": 412, "height": 915}
    x = int(vp["width"] * rng.uniform(0.30, 0.70))
    sign = -1 if direction == "up" else 1
    y_start = int(vp["height"] * (start_y_frac if direction == "up" else 0.25))

    distance = max(80, int(distance_px * _jitter(rng, jitter_pct)))
    steps = rng.randint(8, 14)
    step_ms = max(8, duration_ms // steps)

    # touchstart
    await page.evaluate(
        """([x, y]) => {
            const t = new Touch({ identifier: 1, target: document.elementFromPoint(x, y), clientX: x, clientY: y });
            document.dispatchEvent(new TouchEvent('touchstart', { touches: [t], changedTouches: [t], bubbles: true }));
        }""",
        [x, y_start],
    )

    for i in range(1, steps + 1):
        frac = (i / steps) ** 0.85  # ease-out: fast start, decelerating — like a real flick
        y = int(y_start + sign * distance * frac)
        x_now = int(x + rng.uniform(-6, 6))
        await page.evaluate(
            """([x, y]) => {
                const t = new Touch({ identifier: 1, target: document.elementFromPoint(x, y), clientX: x, clientY: y });
                document.dispatchEvent(new TouchEvent('touchmove', { touches: [t], changedTouches: [t], bubbles: true }));
            }""",
            [x_now, y],
        )
        await asyncio.sleep(step_ms / 1000.0)

    # Finger rests briefly at the edge before releasing (config scroll_gesture.hold_at_edge).
    await asyncio.sleep(max(20, hold_at_edge_ms) / 1000.0)
    y_end = int(y_start + sign * distance)
    await page.evaluate(
        """([x, y]) => {
            const t = new Touch({ identifier: 1, target: document.elementFromPoint(x, y), clientX: x, clientY: y });
            document.dispatchEvent(new TouchEvent('touchend', { touches: [], changedTouches: [t], bubbles: true }));
        }""",
        [x, y_end],
    )


async def tap_with_jitter(
    page,
    element,
    *,
    max_offset_px: float = 8.0,
    rng: random.Random | None = None,
) -> None:
    """Tap near (not exactly on) an element center — humans miss the bullseye."""
    rng = rng or random.Random()
    box = await element.bounding_box()
    if not box:
        raise RuntimeError("tap target has no bounding box (element off-screen?)")
    cx = box["x"] + box["width"] / 2.0
    cy = box["y"] + box["height"] / 2.0
    # Bias toward the lower half of the element: thumbs tap low, not at the geometric center.
    x = cx + rng.uniform(-max_offset_px, max_offset_px)
    y = cy + rng.uniform(0.0, max_offset_px * 1.5)
    await page.touchscreen.tap(x, y)


async def type_human(
    page,
    element,
    text: str,
    *,
    wpm_range: tuple[float, float] = (90.0, 160.0),
    pause_probability: float = 0.35,
    rng: random.Random | None = None,
) -> None:
    """Type at sampled human speed with occasional mid-typing pauses.

    Per-character delay is derived from a WPM drawn once per call (people don't change
    typing speed between keystrokes), plus small per-keystroke noise; with
    ``pause_probability`` there's a chance of a 0.4–1.6 s "thought" pause mid-sentence.
    """
    rng = rng or random.Random()
    wpm = rng.uniform(*wpm_range)
    base_ms = 60_000.0 / max(1.0, wpm)

    await element.click()
    for i, ch in enumerate(text):
        delay = base_ms * rng.uniform(0.7, 1.4) / 1000.0
        if i > 0 and (ch == " " or ch.isupper()) and rng.random() < pause_probability:
            delay += rng.uniform(0.4, 1.6)
        await asyncio.sleep(delay)
        key = {" ": "Space", "\n": "Enter"}.get(ch, ch)
        await page.keyboard.press(key)
