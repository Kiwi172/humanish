"""API route tests: settings CRUD, account detail drift warnings, session stop.

Uses FastAPI TestClient with a real (tmp) project root so config files resolve.
The DB is isolated per test via the conftest `db` fixture pattern — but since the
API creates its own engine from the app's root, we monkeypatch the root to a tmp dir
with a pre-initialized DB.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def client(app_root, db):
    """FastAPI TestClient bound to a tmp DB (the `db` fixture creates it).

    We need the app's root to point at a location where the DB file exists.
    The conftest `db` fixture uses tmp_path — but the API resolves its own path.
    Simplest approach: create the app with the real repo root (for config files)
    and monkeypatch WARMER_DB_PATH to the test DB.
    """
    import os

    from account_warmer.core.db import init_db, new_session_factory
    from account_warmer.api.app import create_app

    # Use a fresh tmp DB for this test.
    tmp_db = Path(db.bind.engine.url.database)  # extract path from the fixture's engine
    # Actually simpler: just use the real app_root but with WARMER_DB_PATH pointing to tmp.
    # The `db` fixture already created tables in its own tmp file. Let's reuse that path.

    db_path = Path(db.bind.engine.url.database)

    old_env = os.environ.get("WARMER_DB_PATH")
    os.environ["WARMER_DB_PATH"] = str(db_path)

    app = create_app(app_root)
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c

    if old_env is None:
        os.environ.pop("WARMER_DB_PATH", None)
    else:
        os.environ["WARMER_DB_PATH"] = old_env


class TestSettingsAPI:
    """Settings overrides must be real config paths (SPEC §10) — validated on write."""

    def test_get_empty(self, client):
        r = client.get("/settings")
        assert r.status_code == 200
        assert r.json() == {}

    def test_schema_lists_overridable_keys(self, client):
        r = client.get("/settings/schema")
        assert r.status_code == 200
        schema = r.json()
        assert "caps.daily_default.likes" in schema
        assert schema["behavior.like_p"] == "float"

    def test_put_and_get(self, client):
        r = client.put("/settings", json={"key": "caps.daily_default.likes", "value": "15"})
        assert r.status_code == 200
        assert r.json() == {"key": "caps.daily_default.likes", "value": "15"}

        r = client.get("/settings")
        assert r.status_code == 200
        assert r.json()["caps.daily_default.likes"] == "15"

    def test_put_overwrite(self, client):
        client.put("/settings", json={"key": "behavior.like_p", "value": "0.1"})
        r = client.put("/settings", json={"key": "behavior.like_p", "value": "0.25"})
        assert r.status_code == 200
        assert client.get("/settings").json()["behavior.like_p"] == "0.25"

    def test_put_rejects_unknown_key(self, client):
        r = client.put("/settings", json={"key": "global_daily_like_cap", "value": "15"})
        assert r.status_code == 400
        assert "overridable keys" in r.json()["detail"]
        assert client.get("/settings").json() == {}

    def test_put_rejects_invalid_value(self, client):
        r = client.put("/settings", json={"key": "caps.daily_default.likes", "value": "banana"})
        assert r.status_code == 400

    def test_put_rejects_inconsistent_value(self, client):
        """end before start would break the scheduler, so the write is refused."""
        r = client.put("/settings", json={"key": "defaults.window.end", "value": "01:00"})
        assert r.status_code == 400

    def test_stored_override_applies_to_loaded_config(self, client, app_root):
        """The whole point: a stored row changes what services load (SPEC §10)."""
        from account_warmer.core.settings_overrides import load_config_with_overrides

        before = load_config_with_overrides(app_root).caps.daily_default.likes
        r = client.put("/settings", json={"key": "caps.daily_default.likes", "value": "3"})
        assert r.status_code == 200
        assert load_config_with_overrides(app_root).caps.daily_default.likes == 3
        assert before != 3

    def test_delete(self, client):
        client.put("/settings", json={"key": "caps.daily_default.saves", "value": "9"})
        r = client.delete("/settings/caps.daily_default.saves")
        assert r.status_code == 200
        assert r.json() == {"deleted": "caps.daily_default.saves"}
        assert client.get("/settings").json() == {}

    def test_delete_missing(self, client):
        r = client.delete("/settings/nonexistent")
        assert r.status_code == 404


class TestAccountDetailDrift:
    def test_drift_warnings_in_response(self, client, db):
        """After recording selector matches with fallback hits, drift appears in API."""
        from account_warmer.core import repositories as repo

        a = repo.create_account(db, platform="instagram", handle="drift_test")
        db.commit()

        # Create real session rows (FK constraint) and record fallback matches.
        for i in range(5):
            sess = repo.create_session(db, account_id=a.id, planned_duration_s=900)
            repo.record_selector_matches(
                db, account_id=a.id, session_id=sess.id,
                matches=[{"group": "like_button", "matched_index": 1}],
            )
        db.commit()

        r = client.get(f"/accounts/{a.id}")
        assert r.status_code == 200
        data = r.json()
        assert "selector_drift_warnings" in data
        # 5 of 5 sessions on fallback → should trigger (threshold=5, window=10)
        assert any("like_button" in w for w in data["selector_drift_warnings"])

    def test_no_drift_when_primary_matches(self, client, db):
        """When primary candidate (index 0) matches, no drift warning."""
        from account_warmer.core import repositories as repo

        a = repo.create_account(db, platform="tiktok", handle="no_drift")
        db.commit()

        for i in range(10):
            sess = repo.create_session(db, account_id=a.id, planned_duration_s=900)
            repo.record_selector_matches(
                db, account_id=a.id, session_id=sess.id,
                matches=[{"group": "like_button", "matched_index": 0}],
            )
        db.commit()

        r = client.get(f"/accounts/{a.id}")
        data = r.json()
        assert data["selector_drift_warnings"] == []


class TestSessionStop:
    def test_stop_running_session_requests_graceful_stop(self, client, db):
        """A live session is asked to stop at its next boundary, not force-closed (SPEC §2.17)."""
        from account_warmer.core import repositories as repo

        a = repo.create_account(db, platform="instagram", handle="stop_test")
        sess = repo.create_session(db, account_id=a.id, planned_duration_s=900)
        repo.start_session(db, sess)
        repo.touch_heartbeat(db, sess.id)  # fresh heartbeat => not stalled
        db.commit()

        r = client.post(f"/sessions/{sess.id}/stop")
        assert r.status_code == 200
        data = r.json()
        assert data["mode"] == "graceful"
        assert data["stalled"] is False
        assert data["status"] == "running"

        # The flag the warming loop polls is set, and the row is still running.
        db.expire_all()
        assert repo.stop_requested(db, sess.id) is True
        assert db.get(repo.Session, sess.id).status == "running"

    def test_stop_stalled_session_closes_directly(self, client, db):
        """A stalled running session (no heartbeat) is closed immediately (SPEC R21)."""
        from account_warmer.core import repositories as repo
        from datetime import datetime, timedelta, timezone
        a = repo.create_account(db, platform="instagram", handle="stop_stalled")
        sess = repo.create_session(db, account_id=a.id, planned_duration_s=900)
        repo.start_session(db, sess)
        stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sess.heartbeat_at = stale
        sess.started_at = stale
        db.commit()

        r = client.post(f"/sessions/{sess.id}/stop")
        assert r.status_code == 200
        data = r.json()
        assert data["mode"] == "closed"
        assert data["stalled"] is True
        assert data["status"] == "aborted"
        assert data["stop_reason"] == "user_stop"

    def test_stop_nonexistent(self, client):
        r = client.post("/sessions/9999/stop")
        assert r.status_code == 404

    def test_stop_already_completed(self, client, db):
        from account_warmer.core import repositories as repo

        a = repo.create_account(db, platform="instagram", handle="stop_done")
        sess = repo.create_session(db, account_id=a.id, planned_duration_s=900)
        repo.start_session(db, sess)
        repo.close_session(db, sess, status="completed", stop_reason="duration_reached")
        db.commit()

        r = client.post(f"/sessions/{sess.id}/stop")
        assert r.status_code == 400


class TestHTMLPages:
    """Smoke-test that all HTML pages render without server errors."""

    def test_dashboard_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Dashboard" in r.text

    def test_accounts_page(self, client):
        r = client.get("/accounts-page")
        assert r.status_code == 200
        assert "Accounts" in r.text

    def test_sessions_page(self, client):
        r = client.get("/sessions-page")
        assert r.status_code == 200
        assert "Sessions" in r.text

    def test_logs_page(self, client):
        r = client.get("/logs-page")
        assert r.status_code == 200
        assert "Event Logs" in r.text

    def test_settings_page(self, client):
        r = client.get("/settings-page")
        assert r.status_code == 200
        assert "Settings" in r.text

    def test_account_detail_page(self, client, db):
        from account_warmer.core import repositories as repo

        a = repo.create_account(db, platform="instagram", handle="page_test")
        db.commit()

        r = client.get(f"/account/{a.id}")
        assert r.status_code == 200
        assert "page_test" in r.text

    def test_healthz(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestAccountsAPI:
    def test_list_accounts(self, client, db):
        from account_warmer.core import repositories as repo

        a = repo.create_account(db, platform="instagram", handle="api_test")
        db.commit()

        r = client.get("/accounts")
        assert r.status_code == 200
        accounts = r.json()
        assert len(accounts) >= 1
        handles = [a["handle"] for a in accounts]
        assert "api_test" in handles

    def test_create_account(self, client):
        r = client.post("/accounts", json={
            "platform": "tiktok",
            "handle": "new_handle_xyz",
            "label": "Test TT",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["handle"] == "new_handle_xyz"

    def test_create_duplicate(self, client):
        client.post("/accounts", json={"platform": "instagram", "handle": "dup_test"})
        r = client.post("/accounts", json={"platform": "instagram", "handle": "dup_test"})
        assert r.status_code == 409

    def test_set_status(self, client, db):
        from account_warmer.core import repositories as repo

        a = repo.create_account(db, platform="instagram", handle="status_test")
        db.commit()

        r = client.patch(f"/accounts/{a.id}/status?status=paused")
        assert r.status_code == 200
        assert r.json()["status"] == "paused"

    def test_set_invalid_status(self, client, db):
        from account_warmer.core import repositories as repo

        a = repo.create_account(db, platform="instagram", handle="bad_status")
        db.commit()

        r = client.patch(f"/accounts/{a.id}/status?status=bogus")
        assert r.status_code == 400


class TestStatsAPI:
    def test_overview(self, client):
        r = client.get("/stats/overview")
        assert r.status_code == 200
        data = r.json()
        assert "totals_24h" in data
        assert "active_sessions" in data

    def test_daily_series(self, client):
        r = client.get("/stats/daily?days=3")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 3
        for row in rows:
            assert "date" in row
            assert "videos" in row


class TestLogsAPI:
    def test_list_logs(self, client, db):
        from account_warmer.core import repositories as repo

        a = repo.create_account(db, platform="instagram", handle="log_test")
        repo.log_event(
            db, level="warning", category="bot_signal",
            message="test warning", account_id=a.id,
        )
        db.commit()

        r = client.get("/logs?level=warning&category=bot_signal")
        assert r.status_code == 200
        events = r.json()
        assert len(events) >= 1
        assert any("test warning" in e["message"] for e in events)
