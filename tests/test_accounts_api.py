"""server/api/accounts.py -- connected accounts over HTTP, exercised via
TestClient (same convention as tests/test_records_api.py /
tests/test_settings_api.py).
"""
from __future__ import annotations

from unittest import mock

from server.core import accounts, users
from server.providers import microsoft_graph as gc


def _login(client, email, password="correct-horse-battery"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


class TestAuthGate:
    def test_unauthenticated_requests_are_401(self, client, org_id, admin):
        assert client.get("/accounts").status_code == 401
        assert client.post("/accounts/microsoft/link").status_code == 401
        assert client.delete("/accounts/not-a-real-id").status_code == 401


class TestListAndLink:
    def test_a_new_user_has_no_connected_accounts(self, client, org_id, admin):
        _login(client, "admin@example.com")
        response = client.get("/accounts")
        assert response.status_code == 200
        assert response.json() == {"accounts": []}

    def test_an_unknown_provider_is_a_400(self, client, org_id, admin):
        _login(client, "admin@example.com")
        response = client.post("/accounts/aol/link")
        assert response.status_code == 400

    def test_starting_a_link_without_a_configured_client_id_is_a_502(
        self, client, org_id, admin, monkeypatch
    ):
        monkeypatch.delenv("MICROSOFT_OAUTH_CLIENT_ID", raising=False)
        _login(client, "admin@example.com")
        response = client.post("/accounts/microsoft/link")
        assert response.status_code == 502

    def test_starting_a_link_creates_a_pending_account_and_returns_an_authorize_url(
        self, client, org_id, admin, monkeypatch
    ):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        _login(client, "admin@example.com")
        response = client.post("/accounts/microsoft/link")
        assert response.status_code == 200
        assert response.json()["authorize_url"].startswith(
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"
        )
        listed = client.get("/accounts").json()["accounts"]
        assert len(listed) == 1
        assert listed[0]["provider"] == "microsoft"
        assert listed[0]["status"] == "pending"


class TestOwnership:
    def test_a_user_only_sees_their_own_accounts(self, client, org_id, admin, member, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        _login(client, "admin@example.com")
        client.post("/accounts/microsoft/link")

        _login(client, "member@example.com")
        response = client.get("/accounts")
        assert response.json() == {"accounts": []}

    def test_a_user_cannot_disconnect_another_users_account(
        self, client, org_id, admin, member, monkeypatch
    ):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        _login(client, "admin@example.com")
        client.post("/accounts/microsoft/link")
        admin_account_id = client.get("/accounts").json()["accounts"][0]["id"]

        _login(client, "member@example.com")
        response = client.delete(f"/accounts/{admin_account_id}")
        assert response.status_code == 404

        _login(client, "admin@example.com")
        assert len(client.get("/accounts").json()["accounts"]) == 1

    def test_a_user_can_disconnect_their_own_account(self, client, org_id, admin, monkeypatch):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        _login(client, "admin@example.com")
        client.post("/accounts/microsoft/link")
        account_id = client.get("/accounts").json()["accounts"][0]["id"]

        response = client.delete(f"/accounts/{account_id}")
        assert response.status_code == 204
        assert client.get("/accounts").json()["accounts"] == []


class TestOauthCallback:
    def test_completing_a_link_redirects_to_the_connected_accounts_page(
        self, client, org_id, admin, monkeypatch
    ):
        monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-123")
        _login(client, "admin@example.com")
        start = client.post("/accounts/microsoft/link").json()
        state = start["authorize_url"].split("state=")[1].split("&")[0]

        monkeypatch.setattr(
            gc, "exchange_code",
            mock.Mock(return_value={"access_token": "at", "refresh_token": "rt"}),
        )
        monkeypatch.setattr(
            gc, "request_with_token", mock.Mock(return_value={"mail": "me@example.com"})
        )

        response = client.get(
            f"/accounts/oauth/callback?state={state}&code=auth-code", follow_redirects=False
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/admin/connected-accounts?linked=1"

        listed = client.get("/accounts").json()["accounts"]
        assert listed[0]["status"] == "active"
        assert listed[0]["email_address"] == "me@example.com"

    def test_an_unknown_state_is_a_400(self, client, org_id, admin):
        _login(client, "admin@example.com")
        response = client.get("/accounts/oauth/callback?state=bogus&code=x")
        assert response.status_code == 400
