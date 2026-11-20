"""Engine + session factory for SQLite (WAL mode) (SPEC §3 process model, §5 storage).

- ``PRAGMA journal_mode=WAL`` — dashboard reads while a warming session writes.
- ``PRAGMA foreign_keys=ON`` per connection (SQLite defaults OFF).
- ``PRAGMA busy_timeout=5000`` — tolerate short write contention instead of failing.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as _OrmSession  # noqa: N812 - alias to avoid models.Session clash
from sqlalchemy.orm import sessionmaker

from .models import Base


def make_engine(db_path: str | Path) -> Engine:
    """Create a SQLite engine with the warmer's required pragmas."""
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},  # one supervisor process; shared across threads/tasks
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()

    # Existing DBs opened by short-lived commands (CLI, warmer_service) get the same
    # column backfill as `warmer init`, so new columns never require a manual step.
    _ensure_columns(engine)
    return engine


def init_db(db_path: str | Path) -> Engine:
    """Create the engine and ensure all tables exist (create_all; no Alembic in v1)."""
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


#: Columns added after the initial schema (SPEC §3: "create_all + a tiny migration script").
#: Each entry: (table, column, SQLite column DDL).
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("sessions", "heartbeat_at", "TEXT"),
    ("sessions", "stop_requested", "INTEGER NOT NULL DEFAULT 0"),
    ("accounts", "comment_tags", "TEXT"),
)


def _ensure_columns(engine: Engine) -> None:
    """Idempotent ALTER TABLE ADD COLUMN for the handful of columns v1 grew.

    SQLite has no `IF NOT EXISTS` for columns, so we read PRAGMA table_info first.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in _MIGRATIONS:
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))



def new_session_factory(engine: Engine, *, expire_on_commit: bool = False):
    return sessionmaker(bind=engine, future=True, expire_on_commit=expire_on_commit)


@contextmanager
def session_scope(
    factory: "sessionmaker[_OrmSession]",
) -> Iterator["_OrmSession"]:
    """Context-managed unit of work: commit on success, rollback on error."""
    db = factory()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
