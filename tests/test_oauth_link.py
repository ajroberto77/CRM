"""server/core/account_link.py + microsoft_oauth.py/google_oauth.py --
the account-link dispatch and PKCE flow. No real network call anywhere.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from server.core import account_link, accounts, users


@pytest.fixture
def linking_user(org_id):
    return users.create_user(
        org_id, email="linker@example.com", name="Linker",
        password="correct-horse-battery", is_admin=True,
    )


class TestDispatch:
    def test_unknown_provider_raises_on_start(self, org_id, linking_user):
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "microsoft")
        with pytest.raises(account_link.LinkError):
            account_link.start_link(org_id, str(account["id"]), "aol", "https://app/callback")

    def test_unknown_oauth_state_raises_on_complete(self, org_id):
        with pytest.raises(account_link.LinkError):
            account_link.complete_link(org_id, "not-a-real-state", "code", "https://app/callback")


class TestMicrosoftLink:
    def test_start_link_returns_a_pkce_authorize_url_with_the_state_in_it(
        self, org_id, linking_user, monkeypatch
    ):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "microsoft")
        url = account_link.start_link(
            org_id, str(account["id"]), "microsoft", "https://app/callback"
        )
        assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "client_id=client-123" in url

    def test_complete_link_activates_the_account_on_success(
        self, org_id, linking_user, monkeypatch
    ):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "microsoft")
        url = account_link.start_link(
            org_id, str(account["id"]), "microsoft", "https://app/callback"
        )
        state = url.split("state=")[1].split("&")[0]

        responses = iter([
            {"access_token": "at", "refresh_token": "rt"},
            {"mail": "me@example.com"},
        ])

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(next(responses)).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = account_link.complete_link(org_id, state, "auth-code", "https://app/callback")

        assert result["status"] == "active"
        assert result["email_address"] == "me@example.com"
        creds = accounts.get_credentials(org_id, str(account["id"]))
        assert creds["payload"]["refresh_token"] == "rt"

    def test_the_oauth_state_is_single_use(self, org_id, linking_user, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "microsoft")
        url = account_link.start_link(
            org_id, str(account["id"]), "microsoft", "https://app/callback"
        )
        state = url.split("state=")[1].split("&")[0]

        responses = iter([
            {"access_token": "at", "refresh_token": "rt"},
            {"mail": "me@example.com"},
        ])

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(next(responses)).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            account_link.complete_link(org_id, state, "auth-code", "https://app/callback")

        with pytest.raises(account_link.LinkError):
            account_link.complete_link(org_id, state, "auth-code", "https://app/callback")

    def test_a_missing_refresh_token_records_status_error_and_raises(
        self, org_id, linking_user, monkeypatch
    ):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "microsoft")
        url = account_link.start_link(
            org_id, str(account["id"]), "microsoft", "https://app/callback"
        )
        state = url.split("state=")[1].split("&")[0]

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"access_token": "at"}).encode(),  # no refresh_token
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(account_link.LinkError):
                account_link.complete_link(org_id, state, "auth-code", "https://app/callback")

        updated = accounts.get_account(org_id, str(account["id"]))
        assert updated["status"] == "error"
        assert "missing tokens" in updated["status_detail"]

    def test_a_failed_token_exchange_records_status_error_not_silence(
        self, org_id, linking_user, monkeypatch
    ):
        import io
        import urllib.error

        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "microsoft")
        url = account_link.start_link(
            org_id, str(account["id"]), "microsoft", "https://app/callback"
        )
        state = url.split("state=")[1].split("&")[0]

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "invalid_grant", {}, io.BytesIO(b"{}")
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(Exception):
                account_link.complete_link(org_id, state, "auth-code", "https://app/callback")

        updated = accounts.get_account(org_id, str(account["id"]))
        assert updated["status"] == "error"
        assert "token exchange failed" in updated["status_detail"]


class TestGoogleLink:
    def test_start_link_requests_offline_access_and_forces_consent(
        self, org_id, linking_user, monkeypatch
    ):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-456")
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "google")
        url = account_link.start_link(org_id, str(account["id"]), "google", "https://app/callback")
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "access_type=offline" in url
        assert "prompt=consent" in url

    def test_complete_link_activates_the_account_on_success(
        self, org_id, linking_user, monkeypatch
    ):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-456")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "google")
        url = account_link.start_link(org_id, str(account["id"]), "google", "https://app/callback")
        state = url.split("state=")[1].split("&")[0]

        responses = iter([
            {"access_token": "at", "refresh_token": "rt"},
            {"email": "me@gmail.example"},
        ])

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(next(responses)).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = account_link.complete_link(org_id, state, "auth-code", "https://app/callback")

        assert result["status"] == "active"
        assert result["email_address"] == "me@gmail.example"


class TestOauthPendingSweep:
    def test_sweep_removes_only_rows_past_the_max_age(self, org_id, linking_user):
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "microsoft")
        state = accounts.create_oauth_pending(org_id, str(account["id"]), "microsoft", "v")
        accounts.sweep_oauth_pending(org_id, max_age_seconds=0)
        assert accounts.peek_oauth_pending(org_id, state) is None

    def test_sweep_leaves_a_fresh_pending_row_alone(self, org_id, linking_user):
        account = accounts.create_pending_account(org_id, str(linking_user["id"]), "microsoft")
        state = accounts.create_oauth_pending(org_id, str(account["id"]), "microsoft", "v")
        accounts.sweep_oauth_pending(org_id, max_age_seconds=900)
        assert accounts.peek_oauth_pending(org_id, state) is not None
