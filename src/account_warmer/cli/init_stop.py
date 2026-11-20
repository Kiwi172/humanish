"""`warmer init`, `warmer stop`."""
from __future__ import annotations

import os
import signal
import time

import typer

from ..core.bootstrap import ensure_layout
from .common import console, resolve_root


def init_command(root: str = typer.Option(None, "--root", help="project root (default: auto-discover)")) -> None:
    """Create data/, logs/, copy .env.example → .env, initialize the DB schema."""
    result = ensure_layout(resolve_root(root))
    console.print(f"[green]initialized project at {result.root}[/]")
    console.print(f"  data dir : {result.data_dir}")
    console.print(f"  db       : {result.db_path}")
    console.print(f"  logs     : {result.log_dir}")
    if result.env_created:
        console.print("  env      : created .env from .env.example — edit proxies/settings there")
    else:
        console.print("  env      : .env already present (kept)")


def stop_command() -> None:
    """Graceful stop of the running supervisor (SIGTERM to pid in data/run.lock)."""
    from ..services.supervisor import Supervisor

    pid = Supervisor.read_pid()
    if pid is None:
        console.print("[yellow]no supervisor running[/] (data/run.lock not found)")
        return

    # Check if the process is still alive.
    try:
        os.kill(pid, 0)  # signal 0 = existence check only
    except OSError:
        console.print(f"[yellow]stale pidfile[/]: process {pid} no longer exists — removing lock")
        from ..core.config import data_dir_from_env, find_project_root, load_env

        root_path = find_project_root()
        load_env(root_path)
        (data_dir_from_env(root_path) / "run.lock").unlink(missing_ok=True)
        return

    console.print(f"[green]sending SIGTERM to supervisor (pid {pid})…[/]")
    os.kill(pid, signal.SIGTERM)

    # Wait up to 15 s for the pidfile to disappear.
    from ..core.config import data_dir_from_env, find_project_root, load_env

    root_path = find_project_root()
    load_env(root_path)
    pidfile = data_dir_from_env(root_path) / "run.lock"
    for _ in range(30):
        if not pidfile.exists():
            console.print("[green]supervisor stopped cleanly[/]")
            return
        time.sleep(0.5)

    console.print("[yellow]supervisor took >15 s to stop — check logs[/]")
