"""server/api/channels.py -- link/list/unlink a connected channel over
HTTP, exercised via TestClient (same convention as
tests/test_accounts_api.py).
"""
from __future__ import annotations

from server.core import user_channels


def _login(client, email, password="correct-horse-battery"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


class TestAuthGate:
    def test_unauthenticated_requests_are_401(self, client, org_id, admin):
        assert client.get("/channels").status_code == 401
        assert client.post("/channels/link", json={"kind": "signal", "value": "x"}).status_code == 401
        assert client.delete("/channels/not-a-real-id").status_code == 401


class TestLinkAndList:
    def test_link_then_list(self, client, org_id, admin):
        _login(client, "admin@example.com")
        response = client.post("/channels/link", json={"kind": "signal", "value": "+15550100123"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["kind"] == "signal"
        assert body["value_raw"] == "+15550100123"

        listing = client.get("/channels")
        assert listing.status_code == 200
        assert len(listing.json()["channels"]) == 1

    def test_an_unknown_kind_is_a_400(self, client, org_id, admin):
        _login(client, "admin@example.com")
        response = client.post("/channels/link", json={"kind": "email", "value": "x@example.com"})
        assert response.status_code == 400

    def test_an_invalid_value_is_a_400(self, client, org_id, admin):
        _login(client, "admin@example.com")
        response = client.post("/channels/link", json={"kind": "telegram", "value": "!!"})
        assert response.status_code == 400

    def test_a_value_already_linked_to_someone_else_is_a_409(self, client, org_id, admin, member):
        user_channels.link_channel(org_id, str(member["id"]), "signal", "+15550100123")
        _login(client, "admin@example.com")
        response = client.post("/channels/link", json={"kind": "signal", "value": "+15550100123"})
        assert response.status_code == 409

    def test_listing_only_shows_the_caller_own_channels(self, client, org_id, admin, member):
        user_channels.link_channel(org_id, str(member["id"]), "signal", "+15550100123")
        _login(client, "admin@example.com")
        listing = client.get("/channels")
        assert listing.json()["channels"] == []


class TestUnlink:
    def test_disconnect_removes_it(self, client, org_id, admin):
        _login(client, "admin@example.com")
        linked = client.post("/channels/link", json={"kind": "signal", "value": "+15550100123"})
        channel_id = linked.json()["id"]

        response = client.delete(f"/channels/{channel_id}")
        assert response.status_code == 204
        assert client.get("/channels").json()["channels"] == []

    def test_cannot_disconnect_someone_elses_channel(self, client, org_id, admin, member):
        channel = user_channels.link_channel(org_id, str(member["id"]), "signal", "+15550100123")
        _login(client, "admin@example.com")
        response = client.delete(f"/channels/{channel['id']}")
        assert response.status_code == 404
        # And it's still there, from the owning user's point of view.
        assert len(user_channels.list_channels(org_id, user_id=str(member["id"]))) == 1
