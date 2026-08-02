"""server/providers/google_api.py -- the shared Google OAuth token client.
No real network call anywhere -- urllib.request.urlopen is mocked, same
convention as tests/test_microsoft_graph.py.
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest import mock

import pytest

from server.core import accounts, users
from server.providers import google_api as ga


class TestExchangeCode:
    def test_always_sends_client_secret_confidential_client(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-123")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data.decode("utf-8")
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"access_token": "at", "refresh_token": "rt"}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = ga.exchange_code(
                code="auth-code", redirect_uri="https://app/callback", code_verifier="verifier"
            )

        assert result == {"access_token": "at", "refresh_token": "rt"}
        assert captured["url"] == ga.TOKEN_URL
        assert "client_secret=shh" in captured["body"]
        assert "code_verifier=verifier" in captured["body"]

    def test_missing_client_secret_raises_without_a_network_call(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-123")
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        with mock.patch("urllib.request.urlopen") as urlopen:
            with pytest.raises(ga.GoogleApiDisabled):
                ga.exchange_code(code="c", redirect_uri="https://app/callback", code_verifier="v")
        urlopen.assert_not_called()


class TestMintAccessToken:
    def test_a_missing_new_refresh_token_leaves_the_stored_one_untouched(
        self, org_id, monkeypatch
    ):
        """Google does not rotate the refresh token on every refresh call
        (unlike Microsoft) -- this is the normal case, not a failure."""
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-123")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
        admin = users.create_user(
            org_id, email="ggl1@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        account = accounts.create_pending_account(org_id, str(admin["id"]), "google")
        accounts.save_credentials(
            org_id, str(account["id"]), "oauth", {"refresh_token": "stable-rt"}
        )

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"access_token": "at"}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            token = ga.mint_access_token(org_id, str(account["id"]))
        assert token == "at"
        creds = accounts.get_credentials(org_id, str(account["id"]))
        assert creds["payload"]["refresh_token"] == "stable-rt"

    def test_no_stored_credentials_raises_disabled(self, org_id, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-123")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
        admin = users.create_user(
            org_id, email="ggl2@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        account = accounts.create_pending_account(org_id, str(admin["id"]), "google")
        with pytest.raises(ga.GoogleApiDisabled):
            ga.mint_access_token(org_id, str(account["id"]))

    def test_a_410_becomes_a_plain_google_api_error_with_status_preserved(
        self, org_id, monkeypatch
    ):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-123")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
        admin = users.create_user(
            org_id, email="ggl3@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        account = accounts.create_pending_account(org_id, str(admin["id"]), "google")
        accounts.save_credentials(org_id, str(account["id"]), "oauth", {"refresh_token": "rt"})

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"access_token": "at"}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            access_token = ga.mint_access_token(org_id, str(account["id"]))

        def fake_urlopen_410(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 410, "Gone", {}, io.BytesIO(b"{}"))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen_410):
            with pytest.raises(ga.GoogleApiError) as exc_info:
                ga.request_with_token("GET", "https://example.test/x", access_token)
        assert exc_info.value.__cause__.status_code == 410
