"""server/providers/microsoft_graph.py -- the shared Graph OAuth token
client. No real network call anywhere -- urllib.request.urlopen is mocked,
same convention as tests/test_http_retry.py and tests/test_llm_router.py.
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest import mock

import pytest

from server.core import accounts
from server.providers import microsoft_graph as gc


def _fake_urlopen(body: dict):
    def _fn(req, timeout=None):
        return mock.Mock(
            __enter__=lambda s: s, __exit__=lambda *a: None,
            read=lambda: json.dumps(body).encode("utf-8"),
        )
    return _fn


class TestExchangeCode:
    def test_sends_the_pkce_parameters_as_form_encoded_body(self, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_SECRET", raising=False)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data.decode("utf-8")
            captured["headers"] = dict(req.header_items())
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(
                    {"access_token": "at", "refresh_token": "rt"}
                ).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = gc.exchange_code(
                code="auth-code", redirect_uri="https://app/callback",
                code_verifier="verifier-xyz", scopes="offline_access User.Read",
            )

        assert result == {"access_token": "at", "refresh_token": "rt"}
        assert captured["url"] == gc.TOKEN_URL
        assert "code_verifier=verifier-xyz" in captured["body"]
        assert "grant_type=authorization_code" in captured["body"]
        assert "client_secret" not in captured["body"]

    def test_sends_client_secret_when_configured(self, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_SECRET", "shh")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data.decode("utf-8")
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"access_token": "at", "refresh_token": "rt"}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            gc.exchange_code(
                code="c", redirect_uri="https://app/callback",
                code_verifier="v", scopes="s",
            )
        assert "client_secret=shh" in captured["body"]

    def test_missing_client_id_raises_without_a_network_call(self, monkeypatch):
        monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
        with mock.patch("urllib.request.urlopen") as urlopen:
            with pytest.raises(gc.GraphDisabled):
                gc.exchange_code(
                    code="c", redirect_uri="https://app/callback",
                    code_verifier="v", scopes="s",
                )
        urlopen.assert_not_called()


class TestMintAccessToken:
    def test_refreshes_using_the_stored_refresh_token(self, org_id, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        from server.core import users

        admin = users.create_user(
            org_id, email="msgraph@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        account = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")
        accounts.save_credentials(
            org_id, str(account["id"]), "oauth",
            {"refresh_token": "old-rt", "scopes": "offline_access"},
        )

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"access_token": "fresh-at"}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            token = gc.mint_access_token(org_id, str(account["id"]))
        assert token == "fresh-at"

    def test_persists_a_rotated_refresh_token(self, org_id, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        from server.core import users

        admin = users.create_user(
            org_id, email="msgraph2@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        account = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")
        accounts.save_credentials(
            org_id, str(account["id"]), "oauth",
            {"refresh_token": "old-rt", "scopes": "offline_access"},
        )

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(
                    {"access_token": "at", "refresh_token": "rotated-rt"}
                ).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            gc.mint_access_token(org_id, str(account["id"]))

        creds = accounts.get_credentials(org_id, str(account["id"]))
        assert creds["payload"]["refresh_token"] == "rotated-rt"

    def test_no_stored_credentials_raises_graph_disabled(self, org_id, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        from server.core import users

        admin = users.create_user(
            org_id, email="msgraph3@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        account = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")
        with pytest.raises(gc.GraphDisabled):
            gc.mint_access_token(org_id, str(account["id"]))

    def test_a_401_on_refresh_becomes_a_plain_graph_error(self, org_id, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        from server.core import users

        admin = users.create_user(
            org_id, email="msgraph4@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        account = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")
        accounts.save_credentials(
            org_id, str(account["id"]), "oauth",
            {"refresh_token": "revoked-rt", "scopes": "offline_access"},
        )

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "invalid_grant", {}, io.BytesIO(b'{"error":"invalid_grant"}')
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(gc.GraphError):
                gc.mint_access_token(org_id, str(account["id"]))


class TestRequest:
    def test_request_resolves_a_relative_path_against_graph_base(self, org_id, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        from server.core import users

        admin = users.create_user(
            org_id, email="msgraph5@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        account = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")
        accounts.save_credentials(
            org_id, str(account["id"]), "oauth",
            {"refresh_token": "rt", "scopes": "offline_access"},
        )

        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            body = {"access_token": "at"} if "/token" in req.full_url else {"value": []}
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(body).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            gc.request("GET", "/me/contacts", org_id, str(account["id"]))
        assert any(url == f"{gc.GRAPH_BASE}/me/contacts" for url in calls)

    def test_request_passes_a_full_url_through_unresolved(self, org_id, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        from server.core import users

        admin = users.create_user(
            org_id, email="msgraph6@example.com", name="A",
            password="correct-horse-battery", is_admin=True,
        )
        account = accounts.create_pending_account(org_id, str(admin["id"]), "microsoft")
        accounts.save_credentials(
            org_id, str(account["id"]), "oauth",
            {"refresh_token": "rt", "scopes": "offline_access"},
        )
        next_link = "https://graph.microsoft.com/v1.0/me/contacts/delta?$skiptoken=abc"
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            body = {"access_token": "at"} if "/token" in req.full_url else {"value": []}
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(body).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            gc.request("GET", next_link, org_id, str(account["id"]))
        assert next_link in calls
