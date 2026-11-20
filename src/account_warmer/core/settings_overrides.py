"""Live config overrides via the ``app_settings`` table (SPEC §5, §10).

The dashboard "Settings" page writes JSON values keyed by a dotted path into
``config/warmer.yaml``'s validated shape (e.g. ``caps.daily_default.likes``). This module
is the single place that reads those rows and patches a loaded :class:`WarmerConfig`
*in memory* so services honour them without a YAML edit or restart.

Only whitelisted leaf paths may be overridden — arbitrary writes must not be able to
corrupt the validated config (e.g. replace a whole model with a string).
"""
from __future__ import annotations

from typing import Any

#: Dotted path -> pydantic field type, used to coerce/validate the stored JSON value.
#: Anything not listed here is ignored (with a debug log) rather than applied.
OVERRIDABLE: dict[str, type] = {
    "defaults.window.start": str,
    "defaults.window.end": str,
    "defaults.session_duration_s.min": int,
    "defaults.session_duration_s.max": int,
    "defaults.quick_check_probability": float,
    "session.quick_check_duration_s": int,
    "session.max_errors_before_abort": int,
    "behavior.like_p": float,
    "behavior.comment_p": float,
    "behavior.save_p": float,
    "behavior.follow_p": float,
    "caps.daily_default.videos": int,
    "caps.daily_default.likes": int,
    "caps.daily_default.comments": int,
    "caps.daily_default.saves": int,
    "caps.daily_default.follows": int,
    "warnings.pause_after.count": int,
    "warnings.pause_after.within_hours": int,
    "warnings.screenshots_on_warning": bool,
}


def _set_path(cfg: Any, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    obj = cfg
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


def _dig(obj: Any, parts: list[str]) -> Any:
    for part in parts:
        obj = obj[part] if isinstance(obj, dict) else getattr(obj, part)
    return obj


def apply_overrides(cfg: Any, settings: dict[str, Any]) -> list[str]:
    """Patch ``cfg`` from a ``{key: value}`` mapping of app settings.

    Values may already be decoded (dicts/ints) or JSON strings — both are accepted.
    Each candidate is checked by re-validating a *copy* of the config, so a bad manual
    edit is skipped rather than being able to take down a warming run or produce an
    inconsistent plan (e.g. a window whose end precedes its start).
    Returns the list of keys actually applied.
    """
    applied: list[str] = []
    for key, raw in settings.items():
        if key not in OVERRIDABLE:
            continue
        value = raw
        if isinstance(value, str):
            try:
                import json as _json

                value = _json.loads(value)
            except (_json.JSONDecodeError, TypeError):
                pass  # treat the raw string as the value (e.g. "08:00")
        expected = OVERRIDABLE[key]
        try:
            if expected is not bool and isinstance(value, bool):
                raise ValueError("bool where number expected")
            coerced = value if isinstance(value, expected) else expected(value)
        except (ValueError, TypeError):
            continue

        # Dry run: apply to a plain dict copy and re-validate before touching the live cfg.
        candidate = cfg.model_dump(mode="json")
        parts = key.split(".")
        try:
            _dig(candidate, parts[:-1])[parts[-1]] = coerced
            type(cfg).model_validate(candidate)
        except Exception:
            continue
        try:
            _set_path(cfg, key, coerced)
        except Exception:  # pragma: no cover - validated above
            continue
        applied.append(key)
    return applied


def load_overrides(db: Any) -> dict[str, Any]:
    """Read all app_settings rows from ``db`` as ``{key: decoded_value}``."""
    from sqlalchemy import select

    from .models import AppSetting
    from .repositories import _decode_setting

    out: dict[str, Any] = {}
    for row in db.scalars(select(AppSetting)).all():
        out[row.key] = _decode_setting(row.value)
    return out


def load_config_with_overrides(root: str | Any = None) -> Any:
    """Load ``config/warmer.yaml`` and apply any live ``app_settings`` overrides.

    This is the entry point services should use instead of
    :func:`~account_warmer.core.config.load_warmer_config` when they want the
    dashboard's live edits to take effect (SPEC §10). Falls back to the plain YAML
    config when the DB is missing or unreadable — a warming run must not die because a
    settings row is malformed.
    """
    from .config import db_path_from_env, find_project_root, load_env, load_warmer_config

    root_path = find_project_root(root)
    load_env(root_path)
    cfg = load_warmer_config(root_path)
    try:
        from .db import make_engine, new_session_factory, session_scope

        engine = make_engine(db_path_from_env(root_path))
        factory = new_session_factory(engine)
        with session_scope(factory) as db:
            settings = load_overrides(db)
    except Exception:  # pragma: no cover - no DB yet (fresh install)
        return cfg
    if settings:
        apply_overrides(cfg, settings)
    return cfg
