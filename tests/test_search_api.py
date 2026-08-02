"""server/api/search.py -- POST /search over HTTP, exercised via
TestClient (same convention as tests/test_records_api.py). The LLM
embedding call is mocked at server/llm/embeddings.py's own boundary.
"""
from __future__ import annotations

from unittest import mock

from server.core import embeddings
from server.db.schema import EMBEDDING_DIM
from server.llm import embeddings as embedding_provider


def _login(client, email, password="correct-horse-battery"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


def _fake_embed(org_id, text):
    return [0.3] * EMBEDDING_DIM, "fake-model"


class TestAuthGate:
    def test_unauthenticated_is_401(self, client, org_id, admin):
        assert client.post("/search", json={"query": "renewal"}).status_code == 401


class TestSearch:
    def test_returns_matching_results(self, client, org_id, admin):
        embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id="11111111-1111-1111-1111-111111111111",
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=0,
            content_text="the renewal is due next month", vector=[0.3] * EMBEDDING_DIM,
            model="fake-model",
        )
        _login(client, "admin@example.com")
        with mock.patch.object(embedding_provider, "embed", side_effect=_fake_embed):
            response = client.post("/search", json={"query": "renewal"})
        assert response.status_code == 200
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["content_text"] == "the renewal is due next month"

    def test_a_different_users_body_derived_result_never_surfaces(
        self, client, org_id, admin, member
    ):
        embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id="11111111-1111-1111-1111-111111111111",
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=0,
            content_text="admin's private email", vector=[0.3] * EMBEDDING_DIM, model="fake-model",
        )
        _login(client, "member@example.com")
        with mock.patch.object(embedding_provider, "embed", side_effect=_fake_embed):
            response = client.post("/search", json={"query": "renewal"})
        assert response.status_code == 200
        assert response.json()["results"] == []

    def test_a_blank_query_is_rejected(self, client, org_id, admin):
        _login(client, "admin@example.com")
        response = client.post("/search", json={"query": ""})
        assert response.status_code == 422

    def test_an_embedding_provider_failure_is_a_400(self, client, org_id, admin):
        _login(client, "admin@example.com")
        with mock.patch.object(
            embedding_provider, "embed",
            side_effect=embedding_provider.EmbeddingError("provider down"),
        ):
            response = client.post("/search", json={"query": "renewal"})
        assert response.status_code == 400
