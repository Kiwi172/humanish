"""`warmer logs …` — filtered view of the event_logs table."""
from __future__ import annotations

from typing import Optional

import typer
from rich.table import Table

from ..core import repositories as repo
from .common import console, db_session, die

_LEVELS = ("debug", "info", "warning", "error")
_CATEGORIES = ("app", "login", "bot_signal", "ad", "selector", "proxy", "network", "limit")


def logs_command(
    level: Optional[str] = typer.Option(None, "--level", help=f"one of {', '.join(_LEVELS)}"),
    category: Optional[str] = typer.Option(None, "--category", help=f"one of {', '.join(_CATEGORIES)}"),
    account: Optional[int] = typer.Option(None, "--account", help="filter to one account id"),
    tail: int = typer.Option(50, "--tail", min=1, max=1000, help="max rows (newest first)"),
) -> None:
    """Query the unified event log (bot-signal warnings, ad skips, selector health, errors)."""
    if level is not None and level not in _LEVELS:
        raise die(f"--level must be one of {', '.join(_LEVELS)}")
    if category is not None and category not in _CATEGORIES:
        raise die(f"--category must be one of {', '.join(_CATEGORIES)}")

    table = Table(title="Event log (newest first)")
    table.add_column("time (UTC)", style="dim")
    table.add_column("level", justify="right")
    table.add_column("category")
    table.add_column("acc", justify="right")
    table.add_column("message", max_width=90)

    with db_session() as db:
        events = repo.query_events(
            db, level=level, category=category, account_id=account, limit=tail
        )
        for e in events:
            style = {"warning": "yellow", "error": "red"}.get(e.level, "dim") if e.level != "info" else ""
            table.add_row(
                (e.created_at or "").replace("T", " ").rstrip("Z"),
                f"[{style}]{e.level}[/{style}]" if style else e.level,
                e.category,
                str(e.account_id) if e.account_id is not None else "-",
                e.message[:90],
            )

    console.print(table)
    if not events:
        console.print("[dim]no matching events[/]")
