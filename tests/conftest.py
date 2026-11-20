"""Shared fixtures: real repo root + isolated tmp SQLite DB per test."""
from __future__ import annotations

from pathlib import Path

import pytest

from account_warmer.core.config import find_project_root
from account_warmer.core.db import init_db, new_session_factory


@pytest.fixture(scope="session")
def app_root() -> Path:
    """The actual repository root (tests/ lives inside it)."""
    return find_project_root(Path(__file__).resolve().parent)


@pytest.fixture()
def db(tmp_path):
    """Fresh SQLite file per test with full schema; open session, no auto-commit."""
    engine = init_db(tmp_path / "test.db")
    factory = new_session_factory(engine)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
