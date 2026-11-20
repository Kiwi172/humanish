"""Repository behavior: dedupe backstops, daily counters (tz-aware), event logging."""
from __future__ import annotations

from datetime import datetime

import pytest

from account_warmer.core import repositories as repo
from account_warmer.core.models import Action


class TestAccounts:
    def test_create_and_duplicate(self, db):
        a1 = repo.create_account(db, platform="instagram", handle="mybrand")
        assert a1.status == "new"
        with pytest.raises(repo.DuplicateAccountError):
            # @-stripped + whitespace-trimmed before the (platform, handle) unique check.
            repo.create_account(db, platform="instagram", handle="@mybrand")

    def test_update_and_remove(self, db):
        a = repo.create_account(db, platform="tiktok", handle="dormant_tik")
        repo.update_account(a, status="paused")
        assert repo.get_account(db, a.id).status == "paused"
        assert repo.remove_account(db, a.id) is True
        assert repo.get_account(db, a.id) is None

    def test_comment_tags_roundtrip(self, db):
        """SPEC §2.10: per-account topic tags persist as JSON and parse back out."""
        a = repo.create_account(db, platform="instagram", handle="tagged",
                                comment_tags=["food", "travel"])
        assert repo.account_comment_tags(a) == ["food", "travel"]

    def test_comment_tags_default_empty(self, db):
        a = repo.create_account(db, platform="instagram", handle="untagged")
        assert a.comment_tags is None
        assert repo.account_comment_tags(a) == []

    def test_comment_tags_tolerates_bad_json(self, db):
        a = repo.create_account(db, platform="instagram", handle="broken_tags")
        a.comment_tags = "{not json"
        assert repo.account_comment_tags(a) == []


class TestSessionControl:
    """Heartbeat + cooperative stop (SPEC §2.17 stop button, R21 stall visibility)."""

    def _running(self, db, handle="ctl"):
        a = repo.create_account(db, platform="instagram", handle=handle)
        s = repo.create_session(db, account_id=a.id, planned_duration_s=900)
        repo.start_session(db, s)
        return a, s

    def test_heartbeat_stamps_running_session(self, db):
        _, s = self._running(db)
        assert s.heartbeat_at is None
        repo.touch_heartbeat(db, s.id)
        assert s.heartbeat_at is not None

    def test_heartbeat_ignored_for_closed_session(self, db):
        _, s = self._running(db, handle="hb_done")
        repo.close_session(db, s, status="completed")
        assert repo.touch_heartbeat(db, s.id) is None
        assert s.heartbeat_at is None

    def test_request_stop_sets_flag_polled_by_loop(self, db):
        _, s = self._running(db, handle="stopper")
        assert repo.stop_requested(db, s.id) is False
        repo.request_stop(db, s.id)
        assert repo.stop_requested(db, s.id) is True

    def test_request_stop_ignored_when_not_running(self, db):
        _, s = self._running(db, handle="stop_done")
        repo.close_session(db, s, status="completed")
        assert repo.request_stop(db, s.id) is None
        assert repo.stop_requested(db, s.id) is False

    def test_stop_requested_missing_session(self, db):
        assert repo.stop_requested(db, 99999) is False


class TestDedupe:
    def test_like_once_per_video(self, db):
        a = repo.create_account(db, platform="instagram", handle="dedupe1")
        first = repo.add_action(db, account_id=a.id, action_type="like", video_key="abc123")
        assert first is not None
        second = repo.add_action(db, account_id=a.id, action_type="like", video_key="abc123")
        assert second is None  # UNIQUE backstop — "already acted", not an error
        assert repo.already_liked(db, a.id, "abc123") is True
        assert repo.already_saved(db, a.id, "abc123") is False

    def test_follow_partial_unique_per_author(self, db):
        """Follows dedupe on (account, target) via partial unique index — video_key stays NULL."""
        a = repo.create_account(db, platform="instagram", handle="dedupe2")
        first = repo.add_action(db, account_id=a.id, action_type="follow", target_handle="creator_x")
        assert first is not None and first.video_key is None
        # Same author again (even with different context) → duplicate.
        second = repo.add_action(db, account_id=a.id, action_type="follow", target_handle="creator_x")
        assert second is None
        assert repo.already_followed(db, a.id, "creator_x") is True
        # Different author still allowed.
        third = repo.add_action(db, account_id=a.id, action_type="follow", target_handle="creator_y")
        assert third is not None

    def test_comment_once_per_video(self, db):
        a = repo.create_account(db, platform="tiktok", handle="dedupe3")
        c1 = repo.add_comment(db, account_id=a.id, video_key="734567890", comment_text="love this")
        assert c1 is not None and c1.template_key is None
        c2 = repo.add_comment(db, account_id=a.id, video_key="734567890", comment_text="again?")
        assert c2 is None
        assert repo.already_commented(db, a.id, "734567890") is True


class TestDailyCounters:
    def test_count_today_utc_account(self, db):
        """Local day == UTC day for tz=UTC; rows outside the calendar day are excluded."""
        now = datetime(2026, 9, 15, 12, 0, 0)  # fixed instant, tz-naive → treated as UTC by timeutil
        a = repo.create_account(db, platform="instagram", handle="count1", timezone_id="UTC")
        db.add(Action(account_id=a.id, action_type="like", video_key="v1", created_at="2026-09-14T23:59:59Z"))
        db.add(Action(account_id=a.id, action_type="like", video_key="v2", created_at="2026-09-15T01:00:00Z"))
        db.add(Action(account_id=a.id, action_type="save", video_key="v2", created_at="2026-09-15T02:00:00Z"))
        db.flush()

        assert repo.count_today(db, a, "like", now=now) == 1   # only the 15th row
        assert repo.count_today(db, a, "save", now=now) == 1
        assert repo.count_today(db, a, "follow", now=now) == 0

    def test_count_today_offset_timezone(self, db):
        """Kiritimati is UTC+14: the local day starts at 10:00Z — boundary must follow it."""
        # Local 2026-09-16 02:00 == 2026-09-15T12:00Z. Local day started 2026-09-15T10:00Z.
        now = datetime(2026, 9, 15, 12, 0, 0)
        a = repo.create_account(db, platform="tiktok", handle="count2", timezone_id="Pacific/Kiritimati")
        db.add(Action(account_id=a.id, action_type="like", video_key="v1", created_at="2026-09-15T09:30:00Z"))  # prev local day
        db.add(Action(account_id=a.id, action_type="like", video_key="v2", created_at="2026-09-15T11:00:00Z"))  # this local day
        db.flush()

        assert repo.count_today(db, a, "like", now=now) == 1


class TestEvents:
    def test_log_event_json_detail_and_window(self, db):
        a = repo.create_account(db, platform="instagram", handle="events")
        ev = repo.log_event(
            db, level="warning", category="bot_signal", account_id=a.id,
            message="unusual activity detected", detail={"matched": "unusual activity"},
        )
        assert ev.detail == '{"matched": "unusual activity"}'
        now = datetime(2026, 9, 15, 12, 0, 0)
        # A second warning with a manually-set old timestamp must fall outside the window.
        from account_warmer.core.models import EventLog

        db.add(EventLog(account_id=a.id, level="warning", category="bot_signal",
                        message="stale", created_at="2026-09-10T00:00:00Z"))
        db.flush()
        assert repo.warnings_in_window(db, a.id, within_hours=48, now=now) == 1
