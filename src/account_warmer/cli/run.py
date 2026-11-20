"""`warmer run …` — start the supervisor loop or a single immediate warming session."""
from __future__ import annotations

import asyncio
from typing import Optional

import typer
from sqlalchemy import select

from ..core import repositories as repo
from .common import console, db_session, die


def run_command(
    account: Optional[int] = typer.Option(None, "--account", help="single account id"),
    all_accounts: bool = typer.Option(False, "--all", help="every active account (supervisor mode)"),
    now: bool = typer.Option(False, "--now", help="run one session immediately instead of waiting for the window"),
    with_dashboard: bool = typer.Option(False, "--with-dashboard", help="serve the FastAPI dashboard in-process"),
) -> None:
    """Start warming (supervisor loop with scheduler; `--account <id> --now` = one session now)."""
    # Exactly one of --all / --account must be given (neither, nor both).
    if (account is None) == (not all_accounts):
        raise die("use exactly one of: --all  |  --account <id>")

    with db_session() as db:
        if account is not None:
            a = repo.get_account(db, account)
            if a is None:
                raise die(f"account {account} not found")
            accounts = [a]
        else:
            from ..core.models import Account

            accounts = list(db.scalars(select(Account).where(Account.status == "active")).all())
        if not accounts:
            raise die("no active accounts to warm — add one and `warmer login --account <id>` first")

    # ------------------------------------------------------------------
    # Mode 1: single immediate session (--account <id> --now)
    # ------------------------------------------------------------------
    if account is not None and now:
        from ..services.warmer_service import run_warming_session

        console.print(
            f"[green]running one session now[/]: {accounts[0].platform}/{accounts[0].handle}(#{account})"
        )
        try:
            outcomes = asyncio.run(run_warming_session(account_ids=[account], now=True))
            for o in outcomes:
                c = o.counters
                console.print(
                    f"[{'green' if o.status == 'completed' else 'yellow'}]session {o.session_id} "
                    f"{o.status}[/] (reason={o.stop_reason}): "
                    f"videos={c.videos_watched} ads={c.ads_skipped} likes={c.likes_given} "
                    f"comments={c.comments_made} saves={c.saves_done} follows={c.follows_done}"
                )
        except KeyboardInterrupt:
            console.print("[yellow]interrupted[/]")
        return

    # ------------------------------------------------------------------
    # Mode 2: supervisor loop (--all, optionally with dashboard)
    # ------------------------------------------------------------------
    from ..services.supervisor import Supervisor

    mode = "supervisor loop"
    extra = " + dashboard" if with_dashboard else ""
    console.print(
        f"[green]starting warmer[/]: {mode}{extra}, {len(accounts)} account(s): "
        + ", ".join(f"{a.platform}/{a.handle}(#{a.id})" for a in accounts)
    )
    if with_dashboard:
        import os

        host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
        port = os.environ.get("DASHBOARD_PORT", "8321")
        console.print(f"[cyan]dashboard:[/] http://{host}:{port}")
    console.print("[dim]press Ctrl+C to stop[/]")

    sup = Supervisor(with_dashboard=with_dashboard)
    try:
        asyncio.run(sup.run())
    except KeyboardInterrupt:
        console.print("[yellow]stopped[/]")
    except Exception as exc:
        from ..services.supervisor import AlreadyRunningError

        if isinstance(exc, AlreadyRunningError):
            raise die(str(exc))
        raise
