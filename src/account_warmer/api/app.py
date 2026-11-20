"""FastAPI factory for the local dashboard (SPEC §2.17).

Step 4: full route modules + Jinja2 templates with Tailwind CDN (no build step)
and light polling. The app is created via :func:`create_app` which resolves the
project root once and stores it on ``app.state.root`` for all routes.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from .routes_accounts import router as accounts_router
from .routes_logs import router as logs_router
from .routes_sessions import router as sessions_router
from .routes_settings import router as settings_router
from .routes_stats import router as stats_router


def create_app(root: str | Path | None = None) -> FastAPI:
    """Build the dashboard app. ``root`` is the project root (auto-discovered if omitted)."""
    from ..core.config import find_project_root
    from ..utils.logging_setup import init_logging

    resolved_root = find_project_root(root)
    # Cover standalone `uvicorn account_warmer.api.app:app` — the CLI callback already does
    # this for `warmer run --with-dashboard` (SPEC §2.16 rotating file logs).
    init_logging(resolved_root)

    app = FastAPI(
        title="Account Warmer",
        version=__version__,
        docs_url="/docs" if __debug__ else None,
    )
    app.state.root = resolved_root

    # Jinja2 templates live alongside this module.
    templates_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    app.state.templates = templates

    # Mount API routers.
    app.include_router(accounts_router)
    app.include_router(sessions_router)
    app.include_router(stats_router)
    app.include_router(logs_router)
    app.include_router(settings_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pragma: no cover - trivial
        return {"status": "ok", "version": __version__}

    # ------------------------------------------------------------------
    # HTML pages (Jinja2 + Tailwind CDN)
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request):
        from ..core import repositories as repo
        from ..services.stats_service import browser_mode, compute_hints
        from .deps import db_session

        with db_session(resolved_root) as db:
            accounts = repo.list_accounts(db)
            running_sessions = [s for s in repo.recent_sessions(db, limit=20) if s.status == "running"]
            recent_events = repo.query_events(db, level="warning", limit=10)
            hints = compute_hints(db, accounts, running_sessions)

        return templates.TemplateResponse(
            request, "dashboard.html",
            {
                "accounts": accounts,
                "running_sessions": running_sessions,
                "recent_warnings": recent_events,
                "hints": hints,
                "browser": browser_mode(),
                "version": __version__,
            },
        )

    @app.get("/accounts-page", response_class=HTMLResponse)
    def page_accounts(request: Request):
        from ..core import repositories as repo
        from .deps import db_session

        with db_session(resolved_root) as db:
            accounts = repo.list_accounts(db)

        return templates.TemplateResponse(
            request, "accounts.html",
            {"accounts": accounts},
        )

    @app.get("/account/{account_id}", response_class=HTMLResponse)
    def page_account_detail(request: Request, account_id: int):
        from ..core import repositories as repo
        from .deps import db_session

        with db_session(resolved_root) as db:
            acct = repo.get_account(db, account_id)
            if acct is None:
                return HTMLResponse("<h1>404 — account not found</h1>", status_code=404)
            sessions = repo.recent_sessions(db, account_id=account_id, limit=15)
            events = repo.query_events(db, account_id=account_id, limit=30)

        return templates.TemplateResponse(
            request, "account_detail.html",
            {"account": acct, "sessions": sessions, "events": events},
        )

    @app.get("/sessions-page", response_class=HTMLResponse)
    def page_sessions(request: Request):
        from ..core import repositories as repo
        from .deps import db_session

        with db_session(resolved_root) as db:
            sessions = repo.recent_sessions(db, limit=50)
            accounts = {a.id: a for a in repo.list_accounts(db)}

        # Attach the stall flag (SPEC R21) for the heartbeat column.
        from .routes_sessions import _is_stalled

        rows = []
        for s in sessions:
            rows.append({
                "id": s.id, "account_id": s.account_id, "status": s.status,
                "stop_reason": s.stop_reason, "started_at": s.started_at,
                "ended_at": s.ended_at, "actual_duration_s": s.actual_duration_s,
                "videos_watched": s.videos_watched, "ads_skipped": s.ads_skipped,
                "likes_given": s.likes_given, "comments_made": s.comments_made,
                "warnings_count": s.warnings_count,
                "heartbeat_at": s.heartbeat_at,
                "stalled": _is_stalled(s) if s.status == "running" else False,
            })

        return templates.TemplateResponse(
            request, "sessions.html",
            {"sessions": rows, "accounts": accounts},
        )

    @app.get("/logs-page", response_class=HTMLResponse)
    def page_logs(request: Request):
        from ..core import repositories as repo
        from .deps import db_session

        with db_session(resolved_root) as db:
            events = repo.query_events(db, limit=100)
            accounts = {a.id: a for a in repo.list_accounts(db)}

        return templates.TemplateResponse(
            request, "logs.html",
            {"events": events, "accounts": accounts},
        )

    @app.get("/settings-page", response_class=HTMLResponse)
    def page_settings(request: Request):
        from sqlalchemy import select

        from ..core.models import AppSetting
        from ..core.settings_overrides import OVERRIDABLE
        from .deps import db_session

        with db_session(resolved_root) as db:
            settings = {}
            for row in db.scalars(select(AppSetting)).all():
                settings[row.key] = row.value

        return templates.TemplateResponse(
            request, "settings.html",
            {
                "settings": settings,
                "schema": {key: value.__name__ for key, value in OVERRIDABLE.items()},  # value is a type
            },
        )

    return app


# Module-level instance for `uvicorn account_warmer.api.app:app`.
app = create_app()
