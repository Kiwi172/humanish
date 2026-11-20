"""Account CRUD + status transitions (shared by CLI and dashboard).

Implemented in Step 2 — the CLI's accounts commands call these directly.
Status lifecycle: new → needs_login ⇄ active → paused / flagged / error
(see SPEC §5 statuses; login flow in :mod:`login_service` sets ``active``).
"""
from __future__ import annotations

from ..core import repositories as repo


def add_account(db, *, platform, handle, **kwargs):  # noqa: ANN001 - thin wrapper over repos
    return repo.create_account(db, platform=platform, handle=handle, **kwargs)


def set_status(db, account_id: int, status: str, **extra_fields) -> None:
    from ..core.models import Account

    account = db.get(Account, account_id)
    if account is None:
        raise LookupError(f"account {account_id} not found")
    repo.update_account(account, status=status, **extra_fields)


def remove(db, account_id: int) -> bool:
    return repo.remove_account(db, account_id)
