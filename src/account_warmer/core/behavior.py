"""Behavior planner: distributions for dwell/scroll/idle, interaction decisions (SPEC §6.4).

All randomness lives here so adapters stay deterministic given a plan step (SPEC §3).
Consumes ``WarmerConfig.behavior`` + the active :class:`RampRow` and exposes:

    sample_dwell(rng, cfg, interact_intent=False) -> float     # skewed distribution
    maybe_idle(rng, cfg) -> bool / idle_range_s(cfg) -> tuple[float, float]
    decide_action(rng, ramp_row, counters, caps) -> str | None  # like/save/comment/follow/None
    pick_comment(rng, pool, tag=None) -> (text, template_key)   # {placeholder} substitution

Plus the built-in adjective list for ``{adjective}`` substitution.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

import yaml

#: Built-in vocabulary for ``{adjective}`` placeholder substitution (kept small on purpose —
#: variety comes from timing and pool choice, not thesaurus sprawl).
ADJECTIVES: tuple[str, ...] = (
    "good", "clean", "smooth", "wild", "soft", "loud", "weird", "perfect",
    "honest", "rare", "bold", "quiet", "sharp", "warm", "silly", "epic",
)

_EMOJI: tuple[str, ...] = ("🔥", "💀", "✨", "😮", "👏")

_PLACEHOLDER_RE = re.compile(r"\{(adjective|emoji)\}")


# ---------------------------------------------------------------------------
# Dwell / idle sampling
# ---------------------------------------------------------------------------

def sample_dwell(rng: random.Random, cfg, *, interact_intent: bool = False) -> float:
    """Sample watch time (seconds) for the current video.

    Base draw is a clamped normal around ``dwell_s.mean`` with ``sigma`` skew; videos we
    intend to interact with are watched longer (multiplier from config); occasional very
    short glances (<2 s) happen at ``glance_probability`` (SPEC §6.4 watch-time realism).
    """
    d = cfg.behavior.dwell_s
    if rng.random() < d.glance_probability:
        return round(rng.uniform(0.8, 1.9), 1)

    value = rng.gauss(d.mean, d.sigma)
    if interact_intent:
        lo, hi = d.interact_multiplier_range.low, d.interact_multiplier_range.high
        value *= rng.uniform(lo, hi)
    return round(min(max(value, d.min), d.max), 1)


def maybe_idle(rng: random.Random, cfg) -> bool:
    """Whether to take a "phone down" pause after this video."""
    return rng.random() < cfg.behavior.idle_burst.probability_per_video


def idle_range_s(cfg) -> tuple[float, float]:
    lo = cfg.behavior.idle_burst.range_s.low
    hi = cfg.behavior.idle_burst.range_s.high
    return (lo, hi)


def sample_idle(rng: random.Random, cfg) -> float:
    """Duration of an idle burst — skewed toward the short end (most pauses are brief)."""
    lo, hi = idle_range_s(cfg)
    return round(rng.triangular(lo, hi, lo + 0.3 * (hi - lo)), 1)


# ---------------------------------------------------------------------------
# Interaction decisions (ramp probabilities + daily caps)
# ---------------------------------------------------------------------------

def decide_action(
    rng: random.Random,
    ramp_row,
    counters: dict[str, int],
    caps: dict[str, int],
    *,
    save_p: float | None = None,
    follow_p: float | None = None,
) -> str | None:
    """Pick at most one interaction for the current video.

    ``counters`` = today's counts {"likes": n, "comments": n, "saves": n, "follows": n};
    ``caps`` = effective daily caps (per-account override > global default). The ramp row
    supplies like/comment probabilities; save/follow come from the global behavior config
    (passed explicitly — no hidden module state). Returns one of
    "like" | "save" | "comment" | "follow" | None.

    Semantics: each candidate action fires independently with its own probability, but at
    most ONE action is taken per video (the first that rolls in) — humans rarely like AND
    comment on the same clip in one pass.
    """
    candidates: list[tuple[float, str]] = []
    if ramp_row.like_p > 0 and counters.get("likes", 0) < caps.get("likes", 0):
        candidates.append((ramp_row.like_p, "like"))
    if ramp_row.comment_p > 0 and counters.get("comments", 0) < caps.get("comments", 0):
        candidates.append((ramp_row.comment_p, "comment"))
    save = _GLOBAL_BEHAVIOR.save_p if save_p is None else save_p
    follow = _GLOBAL_BEHAVIOR.follow_p if follow_p is None else follow_p
    if save > 0 and counters.get("saves", 0) < caps.get("saves", 0):
        candidates.append((save, "save"))
    if follow > 0 and counters.get("follows", 0) < caps.get("follows", 0):
        candidates.append((follow, "follow"))

    for p, kind in candidates:
        if rng.random() < p:
            return kind
    return None


# Module-level stand-in for global behavior probabilities (save/follow), set by services
# from the loaded WarmerConfig before a session runs. Keeps decide_action's signature
# stable and testable without a full config object.
class _BehaviorDefaults:
    save_p: float = 0.03
    follow_p: float = 0.008


_GLOBAL_BEHAVIOR = _BehaviorDefaults()


def set_global_behavior(cfg) -> None:
    """Copy save/follow probabilities from a loaded :class:`WarmerConfig` (services call this)."""
    _GLOBAL_BEHAVIOR.save_p = cfg.behavior.save_p
    _GLOBAL_BEHAVIOR.follow_p = cfg.behavior.follow_p


def build_gesture_params(cfg, platform_cfg=None):
    """Resolve ``behavior.scroll`` + ``behavior.typing`` + platform ``scroll_gesture``
    into adapter :class:`~account_warmer.adapters.base.GestureParams` (SPEC §10).

    The service owns this translation so adapters never read config files (SPEC §4).
    """
    from ..adapters.base import GestureParams

    scroll = cfg.behavior.scroll
    typing = cfg.behavior.typing
    gesture = getattr(platform_cfg, "scroll_gesture", None) if platform_cfg is not None else None

    swipe_lo, swipe_hi = (160, 420)
    hold_lo, hold_hi = (50, 160)
    if gesture is not None:
        swipe_lo = int(gesture.swipe_duration_ms_range.low)
        swipe_hi = int(gesture.swipe_duration_ms_range.high)
        hold_lo = int(gesture.hold_at_edge_ms_range.low)
        hold_hi = int(gesture.hold_at_edge_ms_range.high)

    return GestureParams(
        steps=max(1, int(scroll.steps)),
        step_height_min_px=int(scroll.step_height_range_px.low),
        step_height_max_px=int(scroll.step_height_range_px.high),
        jitter_pct=float(scroll.jitter_pct),
        over_scroll_probability=float(scroll.over_scroll_probability),
        back_scroll_probability=float(scroll.back_scroll_probability),
        swipe_duration_min_ms=swipe_lo,
        swipe_duration_max_ms=swipe_hi,
        hold_at_edge_min_ms=hold_lo,
        hold_at_edge_max_ms=hold_hi,
        typing_wpm_min=float(typing.wpm_range.low),
        typing_wpm_max=float(typing.wpm_range.high),
        typing_pause_probability=float(typing.mid_typing_pause_probability),
    )


# ---------------------------------------------------------------------------
# Comment pool loading + placeholder substitution
# ---------------------------------------------------------------------------

def load_comment_pool(platform: str, root: str | Path | None = None) -> dict[str, list[str]]:
    """Load the comment pool YAML for a platform → {tag: [templates]}.

    File naming: ``comments/ig_comments.yaml`` (instagram), ``comments/tt_comments.yaml`` (tiktok).
    Tags are topic labels (general/food/travel/fitness); "general" is the fallback pool.
    """
    from .config import find_project_root  # local import to avoid cycle at module load

    root_path = Path(root) if root is not None else find_project_root()
    prefix = {"instagram": "ig", "tiktok": "tt"}.get(platform, platform[:2])
    path = root_path / "comments" / f"{prefix}_comments.yaml"
    if not path.exists():
        raise FileNotFoundError(f"missing comment pool: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping of tag -> list[str]")
    for tag, items in data.items():
        if not isinstance(items, list) or not all(isinstance(s, str) for s in items):
            raise ValueError(f"{path}: pool {tag!r} must be a list of strings")
    return {str(k): [str(s) for s in v] for k, v in data.items()}


def _substitute(template: str, rng: random.Random) -> str:
    def _repl(match: re.Match[str]) -> str:
        kind = match.group(1)
        if kind == "adjective":
            return rng.choice(ADJECTIVES)
        # {emoji} is rare by design — most picks leave it out entirely.
        return rng.choice(_EMOJI) if rng.random() < 0.25 else ""

    return _PLACEHOLDER_RE.sub(_repl, template).strip()


def pick_comment(
    rng: random.Random,
    pool: dict[str, list[str]],
    *,
    tag: str | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, str]:
    """Pick one comment (resolved text + the pool tag used as ``template_key``).

    ``tags`` is the account's configured topic list (SPEC §2.10 "topic-tagged pools per
    account") — one tag is chosen at random from the ones that actually exist in the pool,
    which keeps an account's comments varied across its topics instead of always general.
    A single ``tag`` still wins when given (manual/one-off picks). Falls back to "general"
    when nothing usable is configured. Raises :class:`LookupError` when no pool exists.
    """
    chosen_tag = tag
    if chosen_tag is None and tags:
        usable = [t for t in tags if pool.get(t)]
        if usable:
            chosen_tag = rng.choice(usable)
    chosen_tag = chosen_tag if chosen_tag and chosen_tag in pool else "general"
    candidates = pool.get(chosen_tag) or pool.get("general")
    if not candidates:
        raise LookupError(f"no comment templates available for tag {chosen_tag!r}")
    template = rng.choice(candidates)
    return _substitute(template, rng), chosen_tag
