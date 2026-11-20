"""App settings view/edit routes (SPEC §5 app_settings table, SPEC §10).

Writes are validated against the override whitelist in
:mod:`account_warmer.core.settings_overrides`, so the dashboard cannot store a key that
nothing reads (which would silently do nothing) or a value that breaks config validation.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..core import repositories as repo
from ..core.settings_overrides import OVERRIDABLE
from .deps import get_db, get_root

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingUpdate(BaseModel):
    key: str
    value: str  # JSON-encoded or plain string


@router.get("")
def list_settings(db=Depends(get_db)):
    """All app settings (key → value)."""
    from sqlalchemy import select
    from ..core.models import AppSetting

    rows = list(db.scalars(select(AppSetting)).all())
    return {r.key: r.value for r in rows}


@router.get("/schema")
def settings_schema():
    """The overridable config paths and their expected types (drives the UI)."""
    return {key: value.__name__ for key, value in OVERRIDABLE.items()}


@router.put("")
def update_setting(body: SettingUpdate, request: Request, db=Depends(get_db)):
    """Set or overwrite a single app setting, validating key + value."""
    if body.key not in OVERRIDABLE:
        raise HTTPException(
            400,
            f"unknown setting key {body.key!r}; overridable keys: "
            + ", ".join(sorted(OVERRIDABLE)),
        )

    # Round-trip the value through the real config to reject anything invalid.
    from ..core.config import load_warmer_config
    from ..core.settings_overrides import apply_overrides

    probe = load_warmer_config(get_root(request))
    applied = apply_overrides(probe, {body.key: body.value})
    if not applied:
        raise HTTPException(400, f"value {body.value!r} is not valid for {body.key!r}")

    repo.set_setting(db, body.key, body.value)
    return {"key": body.key, "value": body.value}


@router.delete("/{key}")
def delete_setting(key: str, db=Depends(get_db)):
    """Remove an app setting."""
    from ..core.models import AppSetting

    row = db.get(AppSetting, key)
    if row is None:
        raise HTTPException(404, f"setting {key!r} not found")
    db.delete(row)
    db.flush()
    return {"deleted": key}
