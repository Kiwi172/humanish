"""Project bootstrap: runtime dirs, .env seeding, DB schema init (backs `warmer init`)."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine

from .config import db_path_from_env
from .db import init_db


@dataclass(frozen=True)
class BootstrapResult:
    root: Path
    data_dir: Path
    db_path: Path
    log_dir: Path
    env_created: bool


def ensure_layout(root: str | Path, *, create_dirs: bool = True, copy_env_example: bool = True) -> BootstrapResult:
    """Create runtime directories (if needed), seed ``.env`` from the example, init DB schema."""
    root = Path(root).resolve()
    data_dir = root / "data"
    log_dir = root / "logs"
    screenshot_dir = log_dir / "screenshots"

    if create_dirs:
        for d in (data_dir, log_dir, screenshot_dir):
            d.mkdir(parents=True, exist_ok=True)

    env_created = False
    env_path = root / ".env"
    example = root / ".env.example"
    if copy_env_example and not env_path.exists() and example.exists():
        shutil.copyfile(example, env_path)
        env_created = True

    db_path = db_path_from_env(root)
    engine: Engine = init_db(db_path)  # creates parent dirs + tables (idempotent)

    return BootstrapResult(
        root=root,
        data_dir=data_dir,
        db_path=db_path,
        log_dir=log_dir,
        env_created=env_created,
    )
