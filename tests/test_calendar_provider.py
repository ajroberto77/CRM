"""server/providers/calendar.py + microsoft_calendar.py + google_calendar.py
-- R3's calendar axis. No real network call anywhere -- urllib.request.urlopen
is mocked, same convention as tests/test_microsoft_graph.py.

The DST-transition tests are safety rule 7's actual regression coverage:
a "local day" window must be computed via wall-clock arithmetic on an
aware datetime, never a hardcoded 24-hour span.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest import mock

import pytest

from server.core import accounts, users
from server.providers import calendar as cal_dispatch


@pytest.fixture
def microsoft_account(org_id):
    admin = users.create_user(
        org_id, email="calms@example.com", name="A",
        password="correct-horse-battery", is_admin=True,
    )
    acc = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")
    accounts.save_credentials(org_id, str(acc["id"]), "oauth", {"refresh_token": "rt", "scopes": "s"})
    accounts.activate_account(org_id, str(acc["id"]), "me@example.com")
    account = accounts.get_account(org_id, str(acc["id"]))
    account["timezone"] = "America/New_York"
    return account


@pytest.fixture
def google_account(org_id):
    admin = users.create_user(
        org_id, email="calgg@example.com", name="A",
        password="correct-horse-battery", is_admin=True,
    )
    acc = accounts.create_pending_account(org_id, str(admin["id"]), "google")
    accounts.save_credentials(org_id, str(acc["id"]), "oauth", {"refresh_token": "rt"})
    accounts.activate_account(org_id, str(acc["id"]), "me@example.com")
    account = accounts.get_account(org_id, str(acc["id"]))
    account["timezone"] = "America/New_York"
    return account


class TestDispatch:
    def test_unknown_provider_raises(self):
        with pytest.raises(cal_dispatch.CalendarError):
            cal_dispatch.check_event("org", {"provider": "aol"}, "2026-01-01")


class TestDstWindowMath:
    """Direct, network-free tests of the shared window computation in
    calendar.py -- both adapters call this same function, so it's tested
    once here rather than per-adapter."""

    def test_spring_forward_day_spans_23_hours(self):
        _tz_name, start, end = cal_dispatch.local_day_utc_window(
            {"timezone": "America/New_York"}, "2026-03-08"  # US spring-forward
        )
        span = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        assert span == timedelta(hours=23)

    def test_fall_back_day_spans_25_hours(self):
        _tz_name, start, end = cal_dispatch.local_day_utc_window(
            {"timezone": "America/New_York"}, "2026-11-01"  # US fall-back
        )
        span = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        assert span == timedelta(hours=25)

    def test_an_ordinary_day_still_spans_24_hours(self):
        _tz_name, start, end = cal_dispatch.local_day_utc_window(
            {"timezone": "America/New_York"}, "2026-06-15"
        )
        span = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        assert span == timedelta(hours=24)

    def test_invalid_stored_timezone_falls_back_to_utc_without_crashing(self):
        tz_name, _tz = cal_dispatch.account_timezone({"timezone": "Not/A_Real_Zone"})
        assert tz_name == "UTC"


class TestTitleMatching:
    """Also shared: microsoft_calendar.py passes title_field="subject",
    google_calendar.py passes title_field="summary" -- the matching logic
    itself is exercised once here."""

    def test_unambiguous_single_match(self):
        existing = [{"subject": "Dentist Appointment"}, {"subject": "Grocery Run"}]
        assert cal_dispatch.pick_update_target(
            "Dentist", existing, title_field="subject"
        ) == existing[0]

    def test_ambiguous_match_returns_none(self):
        existing = [{"subject": "Team Meeting"}, {"subject": "Client Meeting"}]
        assert cal_dispatch.pick_update_target(
            "Meeting", existing, title_field="subject"
        ) is None

    def test_no_proposed_title_returns_none(self):
        assert cal_dispatch.pick_update_target(
            None, [{"subject": "Anything"}], title_field="subject"
        ) is None

    def test_no_match_returns_none(self):
        existing = [{"subject": "Grocery Run"}]
        assert cal_dispatch.pick_update_target(
            "Dentist", existing, title_field="subject"
        ) is None

    def test_a_different_title_field_name_is_honored(self):
        """google_calendar.py's own field name, "summary"."""
        existing = [{"summary": "Dentist Appointment"}]
        assert cal_dispatch.pick_update_target(
            "Dentist", existing, title_field="summary"
        ) == existing[0]


def _fake_urlopen_factory(responses_by_stage):
    def fake_urlopen(req, timeout=None):
        if "/token" in req.full_url or "oauth2" in req.full_url:
            body = {"access_token": "at"}
        else:
            body = responses_by_stage["response"]
        return mock.Mock(
            __enter__=lambda s: s, __exit__=lambda *a: None,
            read=lambda: json.dumps(body).encode(),
        )
    return fake_urlopen


class TestMicrosoftCalendarWireFormat:
    def test_check_event_computes_a_utc_window_and_sends_the_timezone_header(
        self, org_id, microsoft_account, monkeypatch
    ):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        captured = {}

        def fake_urlopen(req, timeout=None):
            if "/token" in req.full_url:
                return mock.Mock(
                    __enter__=lambda s: s, __exit__=lambda *a: None,
                    read=lambda: json.dumps({"access_token": "at"}).encode(),
                )
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"value": []}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            cal_dispatch.check_event(org_id, microsoft_account, "2026-03-10")

        assert "startDateTime=2026-03-10T04" in captured["url"]  # EDT is UTC-4
        assert "endDateTime=2026-03-11T04" in captured["url"]
        assert captured["headers"].get("Prefer") == 'outlook.timezone="America/New_York"'

    def test_create_event_sends_subject_and_timezone_scoped_start_end(
        self, org_id, microsoft_account, monkeypatch
    ):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        captured = {}

        def fake_urlopen(req, timeout=None):
            if "/token" in req.full_url:
                return mock.Mock(
                    __enter__=lambda s: s, __exit__=lambda *a: None,
                    read=lambda: json.dumps({"access_token": "at"}).encode(),
                )
            captured["body"] = json.loads(req.data.decode())
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"id": "evt-1"}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            event_id = cal_dispatch.create_event(
                org_id, microsoft_account, title="Dentist",
                start_iso="2026-03-10T09:00:00", end_iso="2026-03-10T09:30:00",
            )
        assert event_id == "evt-1"
        assert captured["body"]["subject"] == "Dentist"
        assert captured["body"]["start"]["timeZone"] == "America/New_York"

    def test_update_event_uses_patch_not_put(self, org_id, microsoft_account, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        captured = {}

        def fake_urlopen(req, timeout=None):
            if "/token" in req.full_url:
                return mock.Mock(
                    __enter__=lambda s: s, __exit__=lambda *a: None,
                    read=lambda: json.dumps({"access_token": "at"}).encode(),
                )
            captured["method"] = req.get_method()
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"id": "evt-1"}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            cal_dispatch.update_event(
                org_id, microsoft_account, "evt-1", title="Dentist Rescheduled",
                start_iso="2026-03-10T10:00:00", end_iso="2026-03-10T10:30:00",
            )
        assert captured["method"] == "PATCH"


class TestGoogleCalendarWireFormat:
    def test_check_event_computes_the_same_utc_window(self, org_id, google_account, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-456")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
        captured = {}

        def fake_urlopen(req, timeout=None):
            if "/token" in req.full_url:
                return mock.Mock(
                    __enter__=lambda s: s, __exit__=lambda *a: None,
                    read=lambda: json.dumps({"access_token": "at"}).encode(),
                )
            captured["url"] = req.full_url
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"items": []}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            cal_dispatch.check_event(org_id, google_account, "2026-03-10")
        assert "timeMin=2026-03-10T04" in captured["url"]
        assert "timeMax=2026-03-11T04" in captured["url"]

    def test_create_event_uses_summary_field_and_primary_calendar(
        self, org_id, google_account, monkeypatch
    ):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-456")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
        captured = {}

        def fake_urlopen(req, timeout=None):
            if "/token" in req.full_url:
                return mock.Mock(
                    __enter__=lambda s: s, __exit__=lambda *a: None,
                    read=lambda: json.dumps({"access_token": "at"}).encode(),
                )
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"id": "gevt-1"}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            event_id = cal_dispatch.create_event(
                org_id, google_account, title="Dentist",
                start_iso="2026-03-10T09:00:00", end_iso="2026-03-10T09:30:00",
            )
        assert event_id == "gevt-1"
        assert captured["body"]["summary"] == "Dentist"
        assert "/calendars/primary/events" in captured["url"]
