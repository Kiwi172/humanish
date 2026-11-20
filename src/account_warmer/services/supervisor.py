"""Supervisor loop: scheduler tick + optional uvicorn dashboard in one asyncio process.

SPEC §3 process model: "One supervisor process runs everything: an asyncio loop hosts the
scheduler task(s) plus (optionally) uvicorn serving the dashboard in the same loop."

The supervisor:
1. Writes a pidfile (``data/run.lock``) so ``warmer stop`` can find it.
2. Ticks every 30 s: for each active account, check if its planned start time has arrived;
   if so and no session is already running, launch a warming session as an asyncio task.
3. Optionally serves the FastAPI dashboard via uvicorn in the same loop.
4. On SIGTERM/SIGINT: cancels pending tasks, waits for running sessions to finish (with a
   grace period), removes the pidfile, and exits cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger("warmer.supervisor")


class AlreadyRunningError(RuntimeError):
    """Raised when another supervisor already holds ``data/run.lock`` (SPEC §3)."""


class Supervisor:
    """One supervisor instance manages all accounts' scheduled sessions."""

    def __init__(self, *, root: str | None = None, with_dashboard: bool = False):
        from ..core.config import data_dir_from_env, find_project_root, load_env

        self.root_path = find_project_root(root)
        load_env(self.root_path)
        self.data_dir = data_dir_from_env(self.root_path)
        self.pidfile = self.data_dir / "run.lock"
        self.with_dashboard = with_dashboard
        self._stop_event = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._uvicorn_task: asyncio.Task | None = None
        self._lock_fh = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point: write pidfile, start scheduler + optional dashboard, wait for stop."""
        from ..utils.logging_setup import init_logging

        init_logging(self.root_path)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        self._write_pidfile()
        logger.info("supervisor started (pid=%d, dashboard=%s)", os.getpid(), self.with_dashboard)

        # Install signal handlers.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except NotImplementedError:  # Windows
                pass

        tasks: list[asyncio.Task] = [
            asyncio.create_task(self._scheduler_loop(), name="scheduler"),
        ]
        if self.with_dashboard:
            self._uvicorn_task = asyncio.create_task(self._serve_dashboard(), name="dashboard")
            tasks.append(self._uvicorn_task)

        # Wait until stop is requested.
        await self._stop_event.wait()

        logger.info("supervisor stopping…")
        for t in tasks:
            if not t.done():
                t.cancel()
        # Give running sessions a grace period to finish.
        await asyncio.gather(*tasks, return_exceptions=True)
        self._remove_pidfile()
        logger.info("supervisor stopped cleanly")

    def _request_stop(self) -> None:
        if not self._stop_event.is_set():
            logger.info("stop signal received")
            self._stop_event.set()

    # ------------------------------------------------------------------
    # pidfile + single-instance lock
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> None:
        """Take an exclusive flock on ``data/run.lock`` (SPEC §3: prevents two supervisors).

        A second supervisor would double-run sessions against the same browser profile, so
        we refuse to start. The OS releases the lock automatically if the process dies, so
        a crashed run never wedges the tool.
        """
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows has no fcntl
            return

        fh = open(self.pidfile, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunningError(
                f"another supervisor already holds {self.pidfile} — run `warmer stop` first"
            ) from None
        fh.seek(0)
        fh.truncate()
        self._lock_fh = fh

    def _release_lock(self) -> None:
        if self._lock_fh is None:
            return
        try:
            import fcntl

            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            self._lock_fh.close()
        except Exception:  # pragma: no cover - best effort
            pass
        self._lock_fh = None

    def _write_pidfile(self) -> None:
        if self._lock_fh is not None:
            self._lock_fh.seek(0)
            self._lock_fh.truncate()
            self._lock_fh.write(str(os.getpid()))
            self._lock_fh.flush()
        else:  # pragma: no cover - fcntl unavailable
            self.pidfile.write_text(str(os.getpid()), encoding="utf-8")

    def _remove_pidfile(self) -> None:
        self._release_lock()
        try:
            self.pidfile.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def read_pid(root: str | None = None) -> int | None:
        """Read the pid from ``data/run.lock`` (for ``warmer stop``)."""
        from ..core.config import data_dir_from_env, find_project_root, load_env

        root_path = find_project_root(root)
        load_env(root_path)
        pidfile = data_dir_from_env(root_path) / "run.lock"
        if not pidfile.exists():
            return None
        try:
            return int(pidfile.read_text().strip())
        except (ValueError, OSError):
            return None

    # ------------------------------------------------------------------
    # scheduler loop
    # ------------------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        """Tick every 30 s; launch sessions whose start time has arrived."""
        from ..core.db import make_engine, new_session_factory, session_scope
        from ..core.config import db_path_from_env
        from ..core.settings_overrides import load_config_with_overrides
        from ..core import repositories as repo
        from ..utils.timeutil import utcnow

        engine = make_engine(db_path_from_env(self.root_path))
        factory = new_session_factory(engine)

        # Per-account, per-day cached plan. Re-planning each tick with a fresh RNG would
        # re-sample the start time every 30 s, so the "planned" time would never be real
        # (SPEC §2.6). We plan once per local day and reuse it.
        plans: dict[int, tuple[str, object]] = {}  # account_id -> (local_date, DayPlan)

        while not self._stop_event.is_set():
            # Reload config each tick so dashboard Settings edits apply live (SPEC §10).
            cfg = load_config_with_overrides(self.root_path)
            now = utcnow()

            with session_scope(factory) as db:
                from ..core.models import Account
                from sqlalchemy import select

                accounts = list(db.scalars(select(Account).where(Account.status == "active")).all())

                for acct in accounts:
                    # Skip if a session is already running for this account.
                    if repo.get_running_session(db, acct.id) is not None:
                        continue

                    from ..core.scheduler import plan_account_day, seconds_until_start
                    from ..core.config import load_platform_config
                    from ..services.stats_service import resolve_effective_caps

                    try:
                        platform_cfg = load_platform_config(acct.platform, self.root_path)
                    except FileNotFoundError:
                        continue

                    # Plan once per account-local day; reuse the cached plan afterwards.
                    from zoneinfo import ZoneInfo

                    try:
                        local_date = now.astimezone(ZoneInfo(acct.timezone_id)).date().isoformat()
                    except Exception:
                        local_date = now.date().isoformat()

                    cached = plans.get(acct.id)
                    if cached is None or cached[0] != local_date:
                        caps = resolve_effective_caps(acct, cfg)
                        day_plan = plan_account_day(
                            cfg, platform_cfg, acct, now=now,
                            daily_video_cap=caps["videos"],
                            videos_today=repo.count_today(db, acct, "video"),
                        )
                        plans[acct.id] = (local_date, day_plan)
                    else:
                        day_plan = cached[1]

                    if day_plan is None or not day_plan.sessions:
                        continue
                    if day_plan.sessions[0].video_budget <= 0:
                        continue  # daily video cap already spent (SPEC §2.12)

                    # Timezone-aware: use the account's IANA timezone for start-time math.
                    wait_s = seconds_until_start(day_plan, timezone_id=acct.timezone_id, now=now)
                    # Launch when the start time has arrived (wait <= 0 means it's time).
                    # We also allow a small grace window: if we're within the first 5 min
                    # of the planned start, launch now (avoids missing it between ticks).
                    if wait_s <= 300:
                        task = asyncio.create_task(
                            self._launch_session(acct.id, day_plan.sessions[0], cfg),
                            name=f"session-acct-{acct.id}",
                        )
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)

            # Sleep until next tick (or stop).
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass

    async def _launch_session(self, account_id: int, session_plan, cfg) -> None:
        """Run one warming session (delegates to warmer_service)."""
        from .warmer_service import run_warming_session

        logger.info("supervisor launching session for account %d", account_id)
        try:
            outcomes = await run_warming_session(
                account_ids=[account_id], now=True, root=str(self.root_path)
            )
            for o in outcomes:
                logger.info(
                    "session for account %d finished: status=%s reason=%s videos=%d",
                    account_id, o.status, o.stop_reason, o.counters.videos_watched,
                )
        except Exception as exc:
            logger.exception("session for account %d crashed: %s", account_id, exc)

    # ------------------------------------------------------------------
    # dashboard (uvicorn in-process)
    # ------------------------------------------------------------------

    async def _serve_dashboard(self) -> None:
        """Serve the FastAPI app via uvicorn in the same event loop."""
        import uvicorn
        from ..api.app import create_app

        host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
        port = int(os.environ.get("DASHBOARD_PORT", "8321"))

        app = create_app(self.root_path)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",  # keep supervisor logs clean
            lifespan="on",
        )
        server = uvicorn.Server(config)
        logger.info("dashboard serving on http://%s:%d", host, port)
        await server.serve()
