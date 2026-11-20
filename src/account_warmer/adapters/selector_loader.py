"""Selector loading + validation (SPEC §7).

Python code references *logical keys* only; every concrete selector lives in
``selectors/<platform>_selectors.json`` as ordered candidate lists (+ optional text
patterns / url hints). This module is the single place that parses those files, so a
UI change on IG/TikTok is a config edit, not a code release.

Text patterns are compiled **case-insensitive** (SPEC §7.2 rule 3). Candidate match-index
health logging happens in Step 3 when adapters actually resolve candidates against the DOM.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Every functional group a selector file must define (SPEC §7.1).
REQUIRED_GROUPS: tuple[str, ...] = (
    "feed_container",
    "scroll_target",
    "video_card",
    "video_key_source",
    "like_button",
    "save_button",
    "comment_open",
    "comment_input",
    "comment_send",
    "ad_indicators",
    "ad_close_button",
    "login_indicator",
    "logged_in_indicator",
    "bot_signals",
)

#: Groups whose spec carries a ``candidates`` list (ordered, first match wins).
CANDIDATE_GROUPS = tuple(
    g for g in REQUIRED_GROUPS if g not in ("video_key_source", "ad_indicators", "bot_signals")
)


class SelectorFileError(ValueError):
    """Raised when a selector file fails schema validation; ``errors`` lists every problem."""

    def __init__(self, path: Path, errors: list[str]):
        self.path = path
        self.errors = errors
        super().__init__(f"{path}: " + "; ".join(errors))


@dataclass(frozen=True)
class SelectorGroup:
    """A resolved selector group: ordered CSS candidates + optional compiled text patterns."""

    name: str
    candidates: tuple[str, ...] = ()                 # tried in order (may be empty for text-only groups)
    text_patterns_raw: tuple[str, ...] = ()          # original regex sources (for reporting)
    url_contains: tuple[str, ...] = ()               # login_indicator style URL hints

    #: Compiled case-insensitive patterns; parallel to ``text_patterns_raw``.
    compiled_text_patterns: tuple["re.Pattern[str]", ...] = field(default=(), compare=False)

    #: Nested sub-specs (e.g. like_button.liked_state_indicator) kept as SelectorGroups.
    extras: dict[str, "SelectorGroup"] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class PlatformSelectors:
    """Validated, ready-to-use selector config for one platform."""

    platform: str
    schema_version: int
    updated_at: str | None
    base_urls: dict[str, str]
    groups: dict[str, SelectorGroup]                       # every required group incl. bot_signals subkeys
    video_key_pattern: "re.Pattern[str]"                   # compiled from video_key_source.url_pattern

    def group(self, name: str) -> SelectorGroup:
        try:
            return self.groups[name]
        except KeyError:
            raise KeyError(f"unknown selector group {name!r}") from None


def _compile_ci(patterns: list[str]) -> tuple["re.Pattern[str]", ...]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as exc:  # pragma: no cover - config typos
            raise ValueError(f"bad regex {p!r}: {exc}") from exc
    return tuple(out)


def _build_group(name: str, spec: dict) -> SelectorGroup:
    if not isinstance(spec, dict):
        raise ValueError(f"group {name!r} must be an object")

    candidates = []
    raw_candidates = spec.get("candidates", [])
    if raw_candidates is None:
        raw_candidates = []
    if not isinstance(raw_candidates, list) or not all(isinstance(c, str) and c for c in raw_candidates):
        raise ValueError(f"group {name!r}: 'candidates' must be a non-empty list of strings when present")
    candidates = tuple(raw_candidates)

    text_raw: list[str] = []
    if "text_patterns" in spec:
        tp = spec.get("text_patterns") or []
        if not isinstance(tp, list) or not all(isinstance(t, str) for t in tp):
            raise ValueError(f"group {name!r}: 'text_patterns' must be a list of strings")
        text_raw = list(tp)

    # Several groups express their candidate list under the key "selectors" instead of
    # "candidates": ad_indicators, login_indicator, bot_signals.* (SPEC §7.1 files).
    raw_selectors = spec.get("selectors")
    if raw_selectors is not None:
        if not isinstance(raw_selectors, list) or not all(
            isinstance(s, str) and s for s in raw_selectors
        ):
            raise ValueError(f"group {name!r}: 'selectors' must be a list of non-empty strings when present")
        candidates = tuple(list(raw_selectors) + list(candidates))

    url_contains: list[str] = []
    if "url_contains" in spec:
        uc = spec.get("url_contains") or []
        if not isinstance(uc, list) or not all(isinstance(u, str) and u for u in uc):
            raise ValueError(f"group {name!r}: 'url_contains' must be a list of strings")
        url_contains = list(uc)

    # Nested sub-specs (e.g. like_button.liked_state_indicator) — recurse so adapters can
    # resolve them through the same candidate machinery.
    extras: dict[str, SelectorGroup] = {}
    for key, value in spec.items():
        if key in ("candidates", "selectors", "text_patterns", "url_contains", "note"):
            continue
        if isinstance(value, dict) and (value.get("candidates") or value.get("selectors")):
            try:
                extras[key] = _build_group(f"{name}.{key}", value)
            except ValueError as exc:
                raise ValueError(f"group {name!r} sub-spec {key!r}: {exc}") from None

    compiled = _compile_ci(text_raw)
    return SelectorGroup(
        name=name,
        candidates=candidates,
        text_patterns_raw=tuple(text_raw),
        url_contains=tuple(url_contains),
        compiled_text_patterns=compiled,
        extras=extras,
    )


def validate_spec(raw: dict, *, expected_platform: str | None = None) -> PlatformSelectors:
    """Validate a parsed selector JSON object and build :class:`PlatformSelectors`.

    Raises :class:`SelectorFileError` with **all** problems found (not just the first).
    """
    errors: list[str] = []

    platform = raw.get("platform")
    if not isinstance(platform, str) or not platform:
        errors.append("'platform' missing or not a string")
    elif expected_platform is not None and platform != expected_platform:
        errors.append(f"'platform' is {platform!r} but file name implies {expected_platform!r}")

    schema_version = raw.get("schema_version", 1)
    if not isinstance(schema_version, int) or schema_version < 1:
        errors.append("'schema_version' must be a positive integer")
        schema_version = 1

    base_urls = raw.get("base_urls", {})
    if not isinstance(base_urls, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in base_urls.items()
    ):
        errors.append("'base_urls' must be a mapping of mode -> url string")
        base_urls = {}

    groups: dict[str, SelectorGroup] = {}
    for group_name in REQUIRED_GROUPS:
        if group_name not in raw:
            errors.append(f"missing required group {group_name!r}")
            continue
        try:
            spec = raw[group_name]
            if group_name == "bot_signals":
                # bot_signals maps signal_type -> {text_patterns, selectors}
                if not isinstance(spec, dict) or not spec:
                    errors.append("group 'bot_signals' must be a non-empty mapping of signal_type -> spec")
                    continue
                for sig_type, sub in spec.items():
                    try:
                        groups[f"bot_signals.{sig_type}"] = _build_group(f"bot_signals.{sig_type}", sub)
                    except ValueError as exc:
                        errors.append(str(exc))
            else:
                groups[group_name] = _build_group(group_name, spec)
        except ValueError as exc:
            errors.append(str(exc))

    # Candidate-bearing groups must actually have candidates.
    for group_name in CANDIDATE_GROUPS:
        g = groups.get(group_name)
        if g is not None and not g.candidates:
            errors.append(f"group {group_name!r} needs at least one candidate selector")

    # video_key_source.url_pattern must compile with >= 1 capture group.
    video_key_pattern: "re.Pattern[str] | None" = None
    vks = raw.get("video_key_source", {})
    if isinstance(vks, dict):
        pattern_src = vks.get("url_pattern")
        if not isinstance(pattern_src, str) or not pattern_src:
            errors.append("video_key_source.url_pattern missing or not a string")
        else:
            try:
                video_key_pattern = re.compile(pattern_src)
                if video_key_pattern.groups < 1:
                    errors.append("video_key_source.url_pattern must contain at least one capture group")
            except re.error as exc:
                errors.append(f"video_key_source.url_pattern bad regex: {exc}")

    # login_indicator should have url_contains or selectors (either is acceptable).
    li = groups.get("login_indicator")
    if li is not None and not li.candidates and not li.url_contains:
        errors.append("group 'login_indicator' needs candidates and/or url_contains")

    if errors:
        raise SelectorFileError(Path("<selector spec>"), errors)

    assert video_key_pattern is not None  # guaranteed by validation above
    return PlatformSelectors(
        platform=str(platform),
        schema_version=schema_version,
        updated_at=raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
        base_urls=dict(base_urls),
        groups=groups,
        video_key_pattern=video_key_pattern,
    )


def load_platform_selectors(platform: str, root: str | Path | None = None) -> PlatformSelectors:
    """Load + validate ``selectors/<platform>_selectors.json`` under the project root."""
    from ..core.config import find_project_root  # local import to avoid cycle at module load

    root_path = Path(root) if root is not None else find_project_root()
    path = root_path / "selectors" / f"{platform}_selectors.json"
    if not path.exists():
        raise FileNotFoundError(f"missing selector file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SelectorFileError(path, [f"invalid JSON: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise SelectorFileError(path, ["top level must be a JSON object"])

    try:
        return validate_spec(raw, expected_platform=platform)
    except SelectorFileError as exc:
        exc.path = path  # report the real file path
        raise


def extract_video_key(selectors: PlatformSelectors, url: str) -> tuple[str | None, str | None]:
    """Extract (video_key, author_handle_or_none) from a URL using video_key_source.

    Returns the first capture group as video_key; if there is a second capture group it
    is treated as context (e.g. TikTok author handle comes before the id in its pattern).
    """
    m = selectors.video_key_pattern.search(url or "")
    if not m:
        return None, None
    groups = [g for g in m.groups() if g]
    if not groups:
        return None, None
    # TikTok's pattern is /(@author)/video/(id) -> group order (author, id): video_key is LAST.
    # IG's pattern has a single group -> it IS the video key.
    video_key = groups[-1]
    author = groups[0].lstrip("@") if len(groups) > 1 else None
    return video_key, author
