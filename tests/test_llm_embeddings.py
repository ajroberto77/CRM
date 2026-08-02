"""server/llm/embeddings.py -- urllib.request.urlopen is mocked
throughout (same convention as tests/test_llm_router.py's adapter
tests), no real network call anywhere.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from server.core import settings
from server.db.schema import EMBEDDING_DIM
from server.llm import embeddings


def _fake_response(body: dict):
    return mock.Mock(
        __enter__=lambda s: s, __exit__=lambda *a: None,
        read=lambda: json.dumps(body).encode(),
    )


class TestOllama:
    def test_embeds_via_ollama_by_default(self, org_id):
        vector = [0.1] * EMBEDDING_DIM
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _fake_response({"embedding": vector})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result, model = embeddings.embed(org_id, "hello world")
        assert result == vector
        assert model == "nomic-embed-text"
        assert "/api/embeddings" in captured["url"]
        assert captured["body"]["prompt"] == "hello world"

    def test_a_missing_embedding_field_raises(self, org_id):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None: _fake_response({}),
        ):
            with pytest.raises(embeddings.EmbeddingError):
                embeddings.embed(org_id, "hello")

    def test_a_wrong_dimensional_vector_raises(self, org_id):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None: _fake_response({"embedding": [0.1, 0.2]}),
        ):
            with pytest.raises(embeddings.EmbeddingError):
                embeddings.embed(org_id, "hello")


class TestOpenAI:
    def test_embeds_via_openai_when_thats_the_active_provider(self, org_id, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        settings.update_settings(org_id, "llm", {"provider": "openai"})
        vector = [0.2] * EMBEDDING_DIM
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["headers"] = dict(req.header_items())
            return _fake_response({"data": [{"embedding": vector}]})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result, model = embeddings.embed(org_id, "hello world")
        assert result == vector
        assert model == "text-embedding-3-small"
        assert captured["body"]["input"] == "hello world"
        assert captured["body"]["dimensions"] == EMBEDDING_DIM
        assert captured["headers"]["Authorization"] == "Bearer sk-test"

    def test_missing_api_key_raises(self, org_id, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        settings.update_settings(org_id, "llm", {"provider": "openai"})
        with pytest.raises(embeddings.EmbeddingError):
            embeddings.embed(org_id, "hello")


class TestUnsupportedProvider:
    def test_a_provider_with_no_embeddings_endpoint_raises(self, org_id):
        settings.update_settings(org_id, "llm", {"provider": "anthropic"})
        with pytest.raises(embeddings.EmbeddingError):
            embeddings.embed(org_id, "hello")
