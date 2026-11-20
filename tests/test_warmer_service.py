"""Warming-session control paths that don't need a live browser (SPEC §2.12, §2.17).

The daily-video-cap skip returns *before* Playwright launches, so it is unit-testable.
The stop-flag / heartbeat plumbing is exercised through repositories + the API in
test_api.py / test_repositories.py; here we cover the service-level decision.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture()
def service_db(app_root, tmp_path, monkeypatch):
    """A tmp DB (schema-initialized) with WARMER_DB_PATH pointed at it."""
    from account_warmer.core.db import init_db, new_session_factory, session_scope

    db_path = tmp_path / "warmer_service.db"
    engine = init_db(db_path)
    monkeypatch.setenv("WARMER_DB_PATH", str(db_path))
    monkeypatch.setenv("WARMER_ROOT", str(app_root))
    factory = new_session_factory(engine)
    return factory, session_scope


def test_session_skipped_at_daily_video_cap(app_root, service_db):
    """When today's video cap is spent, the session aborts without launching a browser."""
    from account_warmer.core import repositories as repo
    from account_warmer.services.warmer_service import run_warming_session

    factory, session_scope = service_db
    with session_scope(factory) as db:
        acct = repo.create_account(
            db, platform="instagram", handle="capped", daily_video_cap=5,
        )
        # 5 non-ad videos already watched today == cap.
        for i in range(5):
            repo.record_watched(db, account_id=acct.id, video_key=f"v{i}",
                                watch_seconds=10, session_id=None, was_ad=False)
        acct_id = acct.id

    # Cap of 5 with 5 watched => budget 0 => skip before any browser work.
    outcomes = asyncio.run(run_warming_session(account_ids=[acct_id], now=True, root=str(app_root)))
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.status == "aborted"
    assert o.stop_reason == "daily_video_cap_reached"
    assert o.session_id is None  # no session row was created


def test_session_plans_positive_budget_below_cap(app_root, service_db):
    """Below the cap, the plan carries a positive video budget (no skip)."""
    from account_warmer.core import repositories as repo
    from account_warmer.core.config import load_platform_config, load_warmer_config
    from account_warmer.core.scheduler import plan_account_day

    factory, session_scope = service_db
    with session_scope(factory) as db:
        acct = repo.create_account(
            db, platform="instagram", handle="under_cap", daily_video_cap=50,
        )
        repo.record_watched(db, account_id=acct.id, video_key="v0",
                            watch_seconds=10, session_id=None, was_ad=False)
        acct_id = acct.id

    with session_scope(factory) as db:
        acct = repo.get_account(db, acct_id)
        cfg = load_warmer_config(app_root)
        platform_cfg = load_platform_config("instagram", app_root)
        from account_warmer.services.stats_service import resolve_effective_caps

        caps = resolve_effective_caps(acct, cfg)
        plan = plan_account_day(cfg, platform_cfg, acct,
                                daily_video_cap=caps["videos"], videos_today=1)
        assert plan.sessions[0].video_budget > 0
