"""server/api/proposals.py -- approve/decline over HTTP, exercised via
TestClient (same convention as tests/test_records_api.py).
"""
from __future__ import annotations

from server.core import proposals


def _login(client, email, password="correct-horse-battery"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


class TestAuthGate:
    def test_unauthenticated_is_401(self, client, org_id, admin):
        assert client.post("/proposals/not-a-real-id/approve").status_code == 401
        assert client.post("/proposals/not-a-real-id/decline").status_code == 401


class TestApproveDecline:
    def test_approve_via_http(self, client, org_id, admin):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        _login(client, "admin@example.com")
        response = client.post(f"/proposals/{p['id']}/approve")
        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        assert response.json()["decided_by"].startswith("user:")

    def test_decline_via_http(self, client, org_id, admin):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        _login(client, "admin@example.com")
        response = client.post(f"/proposals/{p['id']}/decline")
        assert response.status_code == 200
        assert response.json()["status"] == "declined"

    def test_double_approve_is_a_409(self, client, org_id, admin):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        _login(client, "admin@example.com")
        client.post(f"/proposals/{p['id']}/approve")
        response = client.post(f"/proposals/{p['id']}/approve")
        assert response.status_code == 409

    def test_listing_pending_proposals_via_the_generic_records_endpoint(
        self, client, org_id, admin
    ):
        proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event",
            payload={"title": "A"},
        )
        p2 = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event",
            payload={"title": "B"},
        )
        proposals.approve(org_id, str(p2["id"]))

        _login(client, "admin@example.com")
        response = client.post(
            "/records/proposed_change/query",
            json={"filters": {"field": "status", "op": "eq", "value": "pending"}},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["records"][0]["payload"]["title"] == "A"

    def test_generic_patch_cannot_bypass_approve_decline(self, client, org_id, admin):
        p = proposals.create(
            org_id, subject_type="interaction", subject_id=None, kind="calendar_event", payload={}
        )
        _login(client, "admin@example.com")
        response = client.patch(
            f"/records/proposed_change/{p['id']}", json={"changes": {"status": "approved"}}
        )
        assert response.status_code == 400
