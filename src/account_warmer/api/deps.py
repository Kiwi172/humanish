"""Shared FastAPI dependencies for the dashboard routes.

Each endpoint gets a fresh DB session via :func:`get_db` (committed on success,
rolled back on exception). The project root is resolved once at app-creation time
and stored in ``app.state.root`` so route handlers don't re-discover it per request.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Request


def get_root(request: Request) -> Path:
    """Project root stored on app.state at factory time."""
    return request.app.state.root  # type: ignore[no-any-return]


@contextmanager
def db_session(root: Path):
    """Yield an active SQLAlchemy session (commits on success)."""
    from ..core.config import db_path_from_env, load_env
    from ..core.db import make_engine, new_session_factory, session_scope

    load_env(root)
    engine = make_engine(db_path_from_env(root))
    factory = new_session_factory(engine)
    with session_scope(factory) as db:
        yield db


def get_db(request: Request):
    """FastAPI dependency: yields a DB session bound to the app's project root."""
    root = get_root(request)
    with db_session(root) as db:
        yield db
