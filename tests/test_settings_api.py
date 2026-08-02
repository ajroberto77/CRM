"""server/api/settings.py -- per-org settings and the LLM status/test-
connection routes, exercised at the HTTP layer via TestClient (same
convention as tests/test_records_api.py).
"""
from __future__ import annotations

from unittest import mock

from server.llm import openai_llm


def _login(client, email="admin@example.com", password="correct-horse-battery"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


class TestAuthGate:
    def test_unauthenticated_requests_are_401(self, client, org_id, admin):
        assert client.get("/settings/llm").status_code == 401
        assert client.patch("/settings/llm", json={"changes": {}}).status_code == 401
        assert client.get("/settings/llm/status").status_code == 401
        assert client.post("/settings/llm/test", json={"overrides": {}}).status_code == 401

    def test_a_non_admin_member_is_forbidden(self, client, org_id, member):
        _login(client, "member@example.com")
        assert client.get("/settings/llm").status_code == 403
        assert client.patch("/settings/llm", json={"changes": {}}).status_code == 403


class TestGetAndUpdate:
    def test_an_unset_section_reads_back_empty(self, client, org_id, admin):
        _login(client)
        response = client.get("/settings/llm")
        assert response.status_code == 200
        assert response.json() == {"section": "llm", "values": {}}

    def test_update_merges_and_a_second_get_reflects_it(self, client, org_id, admin):
        _login(client)
        updated = client.patch("/settings/llm", json={"changes": {"provider": "openai"}})
        assert updated.status_code == 200
        assert updated.json()["values"] == {"provider": "openai"}

        second = client.patch(
            "/settings/llm", json={"changes": {"openai_model": "gpt-4o"}}
        )
        assert second.json()["values"] == {"provider": "openai", "openai_model": "gpt-4o"}

        fetched = client.get("/settings/llm")
        assert fetched.json()["values"] == {"provider": "openai", "openai_model": "gpt-4o"}

    def test_clear_removes_named_keys(self, client, org_id, admin):
        _login(client)
        client.patch("/settings/llm", json={"changes": {"provider": "openai", "openai_model": "gpt-4o"}})
        response = client.patch(
            "/settings/llm", json={"changes": {}, "clear": ["openai_model"]}
        )
        assert response.json()["values"] == {"provider": "openai"}

    def test_an_unknown_section_is_a_400(self, client, org_id, admin):
        _login(client)
        assert client.get("/settings/not-a-real-section").status_code == 400
        assert client.patch(
            "/settings/not-a-real-section", json={"changes": {}}
        ).status_code == 400


class TestLlmStatusAndTest:
    def test_status_reports_every_provider(self, client, org_id, admin):
        _login(client)
        response = client.get("/settings/llm/status")
        assert response.status_code == 200
        providers = response.json()["providers"]
        assert set(providers) == {"ollama", "openai", "anthropic", "gemini", "claudecode"}

    def test_test_connection_requires_a_provider(self, client, org_id, admin):
        _login(client)
        response = client.post("/settings/llm/test", json={"overrides": {}})
        assert response.status_code == 400

    def test_test_connection_uses_overrides_without_persisting_them(
        self, client, org_id, admin, monkeypatch
    ):
        _login(client)
        monkeypatch.setattr(openai_llm, "call", mock.Mock(return_value="ok"))

        response = client.post(
            "/settings/llm/test",
            json={"overrides": {"provider": "openai", "openai_model": "gpt-4o-mini"}},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "error": None}
        assert client.get("/settings/llm").json()["values"] == {}
