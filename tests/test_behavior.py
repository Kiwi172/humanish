"""Unit tests for core/behavior.py — distributions, caps, ramp, comment substitution."""
from __future__ import annotations

import random

import pytest

from account_warmer.core.behavior import (
    ADJECTIVES,
    decide_action,
    idle_range_s,
    load_comment_pool,
    maybe_idle,
    pick_comment,
    sample_dwell,
    sample_idle,
    set_global_behavior,
)


# ---------------------------------------------------------------------------
# Fixtures: minimal config-like objects (avoid loading full YAML in unit tests)
# ---------------------------------------------------------------------------

class _DwellCfg:
    mean = 8.0
    sigma = 3.0
    min = 2.0
    max = 45.0
    interact_multiplier_range = type("P", (), {"low": 1.3, "high": 2.0})()
    glance_probability = 0.1


class _IdleCfg:
    probability_per_video = 0.15
    range_s = type("P", (), {"low": 30.0, "high": 300.0})()


class _BehaviorCfg:
    like_p = 0.12
    comment_p = 0.03
    save_p = 0.04
    follow_p = 0.01
    dwell_s = _DwellCfg()
    idle_burst = _IdleCfg()


class _Cfg:
    behavior = _BehaviorCfg()


CFG = _Cfg()


class _RampRow:
    like_p = 0.15
    comment_p = 0.04


# ---------------------------------------------------------------------------
# sample_dwell
# ---------------------------------------------------------------------------

def test_sample_dwell_within_bounds():
    rng = random.Random(42)
    for _ in range(200):
        v = sample_dwell(rng, CFG)
        assert 0.8 <= v <= 45.0


def test_sample_dwell_interact_longer():
    """Videos we intend to interact with should be watched longer on average."""
    rng1 = random.Random(7)
    rng2 = random.Random(7)
    normal = [sample_dwell(rng1, CFG) for _ in range(500)]
    # Use a separate RNG sequence for interact (same seed but different call pattern).
    interact = []
    rng3 = random.Random(7)
    for _ in range(500):
        # Force non-glance by using high glance prob override? Just sample normally.
        interact.append(sample_dwell(rng3, CFG, interact_intent=True))
    assert sum(interact) / len(interact) > sum(normal) / len(normal)


def test_sample_dwell_glance_short():
    """Glances are < 2s."""
    rng = random.Random(1)
    glances = []
    for _ in range(1000):
        v = sample_dwell(rng, CFG)
        if v < 2.0:
            glances.append(v)
    assert len(glances) > 50  # ~10% of 1000
    assert all(g >= 0.8 for g in glances)


# ---------------------------------------------------------------------------
# maybe_idle / sample_idle
# ---------------------------------------------------------------------------

def test_maybe_idle_probability():
    rng = random.Random(99)
    hits = sum(maybe_idle(rng, CFG) for _ in range(1000))
    # p=0.15 → expect ~150 ± tolerance
    assert 80 < hits < 220


def test_sample_idle_within_range():
    rng = random.Random(5)
    lo, hi = idle_range_s(CFG)
    for _ in range(100):
        v = sample_idle(rng, CFG)
        assert lo <= v <= hi


# ---------------------------------------------------------------------------
# decide_action
# ---------------------------------------------------------------------------

def test_decide_action_respects_caps():
    """When counters hit caps, that action should never fire."""
    rng = random.Random(123)
    ramp = _RampRow()
    # Likes cap reached → no likes possible.
    counters = {"likes": 999, "comments": 0, "saves": 0, "follows": 0}
    caps = {"likes": 5, "comments": 10, "saves": 10, "follows": 10}
    results = [decide_action(rng, ramp, counters, caps) for _ in range(200)]
    assert "like" not in results


def test_decide_action_returns_none_when_all_capped():
    rng = random.Random(456)
    ramp = _RampRow()
    counters = {"likes": 99, "comments": 99, "saves": 99, "follows": 99}
    caps = {"likes": 10, "comments": 10, "saves": 10, "follows": 10}
    for _ in range(50):
        assert decide_action(rng, ramp, counters, caps) is None


def test_decide_action_at_most_one_per_video():
    """Each call returns at most one action (by design)."""
    rng = random.Random(789)
    ramp = _RampRow()
    counters = {"likes": 0, "comments": 0, "saves": 0, "follows": 0}
    caps = {"likes": 100, "comments": 100, "saves": 100, "follows": 100}
    valid = {"like", "save", "comment", "follow", None}
    for _ in range(200):
        result = decide_action(rng, ramp, counters, caps)
        assert result in valid


def test_decide_action_zero_probability_never_fires():
    rng = random.Random(321)

    class _ZeroRamp:
        like_p = 0.0
        comment_p = 0.0

    counters = {"likes": 0, "comments": 0, "saves": 0, "follows": 0}
    caps = {"likes": 10, "comments": 10, "saves": 10, "follows": 10}
    # With save_p/follow_p at defaults (non-zero), those can still fire.
    # Set them to zero explicitly.
    for _ in range(100):
        result = decide_action(rng, _ZeroRamp(), counters, caps, save_p=0.0, follow_p=0.0)
        assert result is None


# ---------------------------------------------------------------------------
# pick_comment + placeholder substitution
# ---------------------------------------------------------------------------

def test_pick_comment_returns_text_and_tag():
    rng = random.Random(10)
    pool = {"general": ["this is {adjective}!", "love this {emoji}", "great video"]}
    text, tag = pick_comment(rng, pool)
    assert tag == "general"
    # No unresolved placeholders.
    assert "{" not in text and "}" not in text


def test_pick_comment_adjective_substituted():
    rng = random.Random(20)
    pool = {"general": ["{adjective} vibes"]}
    for _ in range(50):
        text, _ = pick_comment(rng, pool)
        assert any(adj in text for adj in ADJECTIVES)


def test_pick_comment_fallback_to_general():
    rng = random.Random(30)
    pool = {"general": ["hello"], "food": ["yum"]}
    # Request a tag that doesn't exist → falls back to general.
    text, tag = pick_comment(rng, pool, tag="travel")
    assert tag == "general"
    assert text == "hello"


def test_pick_comment_specific_tag():
    rng = random.Random(40)
    pool = {"general": ["hello"], "food": ["yum {adjective}"]}
    text, tag = pick_comment(rng, pool, tag="food")
    assert tag == "food"
    assert "yum" in text


def test_pick_comment_empty_pool_raises():
    rng = random.Random(50)
    with pytest.raises(LookupError):
        pick_comment(rng, {})


def test_pick_comment_uses_account_topic_tags():
    """SPEC §2.10: an account's configured tags drive which pool is used."""
    rng = random.Random(11)
    pool = {"general": ["hello"], "food": ["yum"], "travel": ["wander"]}
    seen = set()
    for _ in range(60):
        text, tag = pick_comment(rng, pool, tags=["food", "travel"])
        assert tag in ("food", "travel")
        seen.add(tag)
    assert seen == {"food", "travel"}  # both topics get used over many picks


def test_pick_comment_tags_skip_missing_pools():
    rng = random.Random(12)
    pool = {"general": ["hello"], "food": ["yum"]}
    for _ in range(20):
        text, tag = pick_comment(rng, pool, tags=["food", "nonexistent"])
        assert tag == "food"


def test_pick_comment_empty_tags_fall_back_to_general():
    rng = random.Random(13)
    pool = {"general": ["hello"], "food": ["yum"]}
    text, tag = pick_comment(rng, pool, tags=[])
    assert tag == "general"


def test_pick_comment_explicit_tag_beats_tags_list():
    rng = random.Random(14)
    pool = {"general": ["hello"], "food": ["yum"], "travel": ["wander"]}
    text, tag = pick_comment(rng, pool, tag="travel", tags=["food"])
    assert tag == "travel"


# ---------------------------------------------------------------------------
# load_comment_pool (real files from repo)
# ---------------------------------------------------------------------------

def test_load_comment_pool_ig():
    pool = load_comment_pool("instagram")
    assert "general" in pool
    assert all(isinstance(s, str) for s in pool["general"])
    assert len(pool["general"]) > 0


def test_load_comment_pool_tiktok():
    pool = load_comment_pool("tiktok")
    assert "general" in pool
    assert len(pool["general"]) > 0
