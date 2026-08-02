"""server/core/settings.py -- the one per-org, DB-backed product-settings
module (R5), and the read-merge-write choke point safety rule 2 requires.
"""
from __future__ import annotations

import pytest

from server.core import settings


class TestGetSettings:
    def test_an_unset_section_returns_an_empty_dict_not_none(self, org_id):
        assert settings.get_settings(org_id, "llm") == {}

    def test_an_unknown_section_raises(self, org_id):
        with pytest.raises(settings.UnknownSection):
            settings.get_settings(org_id, "not-a-real-section")


class TestUpdateSettings:
    def test_first_write_creates_the_row(self, org_id):
        result = settings.update_settings(org_id, "llm", {"provider": "ollama"})
        assert result == {"provider": "ollama"}
        assert settings.get_settings(org_id, "llm") == {"provider": "ollama"}

    def test_a_second_write_merges_rather_than_replacing(self, org_id):
        settings.update_settings(org_id, "llm", {"provider": "ollama", "ollama_model": "llama3"})
        result = settings.update_settings(org_id, "llm", {"anthropic_model": "claude-opus"})
        assert result == {
            "provider": "ollama", "ollama_model": "llama3", "anthropic_model": "claude-opus",
        }

    def test_clear_removes_named_keys_without_touching_the_rest(self, org_id):
        settings.update_settings(org_id, "llm", {
            "provider": "openai", "openai_model": "gpt-4o", "openai_api_key_hint": "sk-...",
        })
        result = settings.update_settings(
            org_id, "llm", {"provider": "anthropic"}, clear=["openai_api_key_hint"]
        )
        assert result == {"provider": "anthropic", "openai_model": "gpt-4o"}

    def test_a_read_merge_write_can_blank_every_key_when_asked_to(self, org_id):
        """Safety rule 2: never load a subset and write it back as the whole
        file -- but an explicit, named clear must still be able to remove a
        key entirely, distinct from silently dropping keys the caller never
        mentioned."""
        settings.update_settings(org_id, "llm", {"provider": "openai", "openai_model": "gpt-4o"})
        result = settings.update_settings(
            org_id, "llm", {}, clear=["provider", "openai_model"]
        )
        assert result == {}
        assert settings.get_settings(org_id, "llm") == {}

    def test_an_unrelated_write_never_touches_keys_it_did_not_mention(self, org_id):
        """The exact defect this module exists to structurally prevent:
        writing back a subset must never blank fields the caller didn't
        touch."""
        settings.update_settings(org_id, "llm", {
            "provider": "ollama", "ollama_host": "gpu-box", "ollama_port": "11434",
            "ollama_model": "llama3", "chain": [{"provider": "ollama", "model": "llama3"}],
        })
        settings.update_settings(org_id, "llm", {"provider": "anthropic"})
        result = settings.get_settings(org_id, "llm")
        assert result["ollama_host"] == "gpu-box"
        assert result["ollama_port"] == "11434"
        assert result["ollama_model"] == "llama3"
        assert result["chain"] == [{"provider": "ollama", "model": "llama3"}]
        assert result["provider"] == "anthropic"

    def test_settings_are_isolated_per_org(self, org_id, org):
        from server.core import users

        other_org = users.create_org("Other Org", "other-org")
        other_org_id = str(other_org["id"])

        settings.update_settings(org_id, "llm", {"provider": "openai"})
        settings.update_settings(other_org_id, "llm", {"provider": "anthropic"})

        assert settings.get_settings(org_id, "llm")["provider"] == "openai"
        assert settings.get_settings(other_org_id, "llm")["provider"] == "anthropic"

    def test_an_unknown_section_raises_before_writing_anything(self, org_id):
        with pytest.raises(settings.UnknownSection):
            settings.update_settings(org_id, "not-a-real-section", {"x": "y"})
        assert settings.get_settings(org_id, "llm") == {}
