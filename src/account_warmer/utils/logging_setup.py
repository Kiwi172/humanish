"""Stdlib logging setup: rotating file under ``logs/`` + console handler (SPEC §3 tech stack)."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "warmer"
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(log_dir: str | Path, level: str = "INFO") -> logging.Logger:
    """Configure (and return) the app logger. Idempotent — safe to call twice."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    # Replace any previous handlers (idempotency across CLI invocations/tests).
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - best effort cleanup
            pass

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = RotatingFileHandler(
        log_dir / "warmer.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    return logger


def get_logger() -> logging.Logger:
    """Return the app logger (call :func:`setup_logging` first in long-lived processes)."""
    return logging.getLogger(LOGGER_NAME)


def init_logging(root: str | Path | None = None, *, level: str | None = None) -> logging.Logger:
    """Best-effort logging bootstrap for entry points (CLI, supervisor, dashboard).

    Resolves the log directory + level from ``.env`` (``WARMER_LOG_DIR`` / ``LOG_LEVEL``)
    and installs the rotating file handler (SPEC §2.16 "rotating file logs"). Never
    raises: a logging problem must not break the command that triggered it, so on failure
    we fall back to a console-only logger.
    """
    try:
        from ..core.config import find_project_root, load_env, log_dir_from_env

        root_path = find_project_root(root)
        load_env(root_path)
        log_level = level or os.environ.get("LOG_LEVEL", "INFO")
        return setup_logging(log_dir_from_env(root_path), level=log_level)
    except Exception:  # pragma: no cover - project root missing / unwritable logs
        logger = logging.getLogger(LOGGER_NAME)
        if not logger.handlers:
            logger.addHandler(logging.StreamHandler(sys.stderr))
        return logger
