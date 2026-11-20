"""`warmer stats …` — counters vs caps, warnings, selector health."""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

import typer
from rich.table import Table

from ..core import repositories as repo
from ..utils.timeutil import format_iso, utcnow
from .common import console, db_session, die


def stats_command(
    account: Optional[int] = typer.Option(None, "--account", help="single account id"),
    days: int = typer.Option(7, "--days", min=1, max=90, help="trailing window in days"),
) -> None:
    """Counters over the trailing window + today's usage vs caps for each account."""
    since_iso = format_iso(utcnow() - timedelta(days=days))

    table = Table(title=f"Warmer stats — last {days} day(s)")
    table.add_column("id")
    table.add_column("account")
    table.add_column("status")
    for col in ("videos", "likes", "comments", "saves", "follows", "ads", "sessions"):
        table.add_column(col, justify="right")
    for col in ("warnings", "errors"):
        table.add_column(col, justify="right", style="yellow" if col == "warnings" else "red")

    with db_session() as db:
        accounts = repo.list_accounts(db)
        if account is not None:
            match = [a for a in accounts if a.id == account]
            if not match:
                raise die(f"account {account} not found")
            accounts = match

        from ..core.settings_overrides import load_config_with_overrides
        from ..services.stats_service import resolve_effective_caps

        cfg = load_config_with_overrides()
        for a in accounts:
            totals = repo.period_totals(db, account_id=a.id, since_iso=since_iso)
            caps = resolve_effective_caps(a, cfg)
            videos_today = repo.count_today(db, a, "video")
            likes_today = repo.count_today(db, a, "like")
            table.add_row(
                str(a.id),
                f"{a.platform}/{a.handle}",
                a.status,
                f"{totals['videos']} ({videos_today}/{caps['videos']})",
                f"{totals['like']} ({likes_today}/{caps['likes']})",
                str(totals["comments"]),
                str(totals["save"]),
                str(totals["follow"]),
                str(totals["ads"]),
                str(totals["sessions"]),
                str(totals["warnings"]),
                str(totals["errors"]),
            )

    console.print(table)
    console.print("[dim]per-column: period total (today/cap where caps apply)[/]")
