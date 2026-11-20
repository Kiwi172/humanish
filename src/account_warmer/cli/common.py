"""Shared CLI plumbing: root resolution, DB session scope, console output helpers."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import typer
from rich.console import Console
from sqlalchemy.engine import Engine

console = Console()


def resolve_root(root: str | None = None) -> Path:
    """Project root from --root > WARMER_ROOT env > cwd walk-up."""
    from ..core.config import find_project_root

    return find_project_root(root)


@contextmanager
def db_session(root: str | None = None, *, init_if_missing: bool = True) -> Iterator:
    """Engine + unit of work for one CLI invocation (commits on success).

    Loads ``.env`` first so data-dir/DB overrides apply. With ``init_if_missing`` the DB
    file is created on demand — short-lived commands should not fail just because the
    operator has never run `warmer init`.
    """
    root_path = resolve_root(root)
    from ..core.config import db_path_from_env, load_env
    from ..core.db import make_engine, new_session_factory, session_scope

    load_env(root_path)
    engine: Engine = make_engine(db_path_from_env(root_path))  # creates parent dirs + pragmas
    factory = new_session_factory(engine)
    with session_scope(factory) as db:
        yield db


def die(message: str, *, exit_code: int = 1, style: str = "red"):
    """Print an error and exit (usage: ``raise die("...")``)."""
    console.print(f"[{style}]{message}[/{style}]")
    raise typer.Exit(exit_code)
