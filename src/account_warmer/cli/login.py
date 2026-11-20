"""`warmer login --account <id>` / `warmer check-login --account <id>`."""
from __future__ import annotations

import asyncio

import typer

from ..core import repositories as repo
from ..core.fingerprint import build_fingerprint, get_preset
from ..services import login_service
from .common import console, db_session, die, resolve_root


def _load_account(db, account_id: int):
    a = repo.get_account(db, account_id)
    if a is None:
        raise die(f"account {account_id} not found")
    return a


def login_command(
    account: int = typer.Option(..., "--account", help="account id"),
    timeout: float = typer.Option(900.0, "--timeout", help="max seconds to wait for the human login"),
) -> None:
    """Open a visible browser on the login page; you log in (password/QR/2FA); state is verified & persisted."""
    from ..browser.context import profile_dir_for
    from ..core.config import data_dir_from_env

    with db_session() as db:
        a = _load_account(db, account)
        fp = build_fingerprint(a)
        preset = get_preset(a.device_profile)
        root = resolve_root()
        profile_dir = profile_dir_for(str(data_dir_from_env(root)), a.id, a.platform)

    console.print(f"[bold]logging in {a.platform}/{a.handle}[/] (account #{a.id})")
    console.print(f"  fingerprint : {fp.user_agent[:60]}…")
    console.print(f"  tz / locale : {fp.timezone_id} / {fp.locale}")
    if preset.flagged:
        console.print(f"[yellow]  note        : {preset.note}[/]")
    console.print(f"  profile dir : {profile_dir}")

    try:
        asyncio.run(login_service.run_manual_login(account_id=a.id, timeout_s=timeout))
    except NotImplementedError as exc:
        console.print(f"[yellow]placeholder:[/] {exc}")
        console.print("Step 3 opens the real browser here and marks the account active on verified login.")


def check_login_command(
    account: int = typer.Option(..., "--account", help="account id"),
) -> None:
    """Quick session-state probe (does it still look logged in?)."""
    with db_session() as db:
        a = _load_account(db, account)

    console.print(f"[bold]probing login state for {a.platform}/{a.handle}[/] (#{a.id})…")
    try:
        is_logged_in = asyncio.run(login_service.probe_login_state(account_id=a.id))
    except NotImplementedError as exc:
        console.print(f"[yellow]placeholder:[/] {exc}")
        return

    if is_logged_in:
        console.print("[green]✓ session still valid — account looks logged in[/]")
    else:
        console.print("[red]✗ login wall detected — run `warmer login --account " + str(a.id) + "` to re-authenticate[/]")
