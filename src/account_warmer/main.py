"""Typer app root — wires subcommands (console script ``warmer``).

Layout keeps CLI thin: each command in :mod:`account_warmer.cli.*` calls core/services;
no business logic lives here. See SPEC §9 for the command reference.
"""
from __future__ import annotations

import typer

from . import __version__
from .cli.accounts import accounts_app
from .cli.check_selectors import check_selectors_command
from .cli.init_stop import init_command, stop_command
from .cli.login import check_login_command, login_command
from .cli.logs import logs_command
from .cli.run import run_command
from .cli.stats import stats_command

app = typer.Typer(
    name="warmer",
    help=f"Account Warmer v{__version__} — human-like Instagram/TikTok feed warming (see SPEC.md).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.callback()
def _root_callback() -> None:
    """Enable rotating file + console logging for every CLI invocation (SPEC §2.16)."""
    from .utils.logging_setup import init_logging

    init_logging()


app.add_typer(accounts_app, name="accounts")

# Register the typed command functions directly so their typer.Option parameters are exposed.
app.command("init")(init_command)
app.command("stop")(stop_command)
app.command("login")(login_command)
app.command("check-login")(check_login_command)
app.command("run")(run_command)
app.command("stats")(stats_command)
app.command("logs")(logs_command)
app.command("check-selectors")(check_selectors_command)
