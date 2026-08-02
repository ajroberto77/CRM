"""server/llm/router.py -- R3's LLM axis dispatcher -- and its five adapters
(ollama, openai, anthropic, gemini, claudecode).

No real vendor call anywhere -- urllib.request.urlopen is mocked, same
convention as tests/test_http_retry.py and tests/test_esign.py. Router-level
tests exercise a real per-org core.settings row via the org_id fixture
(resolved_chain/call_llm/extract_structured all read settings by org_id);
adapter-level tests call the adapter functions directly with a plain
settings dict, since the adapter contract takes settings as data, not an
org lookup.
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest import mock

import pytest

from server.core import settings as core_settings
from server.llm import anthropic_llm, claudecode_llm, gemini_llm, ollama_llm, openai_llm, router
from server.net import http_retry

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}


class TestClassifyHttpError:
    @pytest.mark.parametrize("status", [401, 402, 403, 408, 429, 500, 502, 503, 504])
    def test_unavailable_statuses_become_provider_unavailable(self, status):
        exc = http_retry.HTTPRetryError("boom", status_code=status)
        assert isinstance(router.classify_http_error(exc), router.ProviderUnavailable)

    @pytest.mark.parametrize("status", [400, 404, 409, 422])
    def test_other_statuses_become_a_plain_llm_error_not_unavailable(self, status):
        exc = http_retry.HTTPRetryError("boom", status_code=status)
        classified = router.classify_http_error(exc)
        assert isinstance(classified, router.LLMError)
        assert not isinstance(classified, router.ProviderUnavailable)

    def test_no_status_code_at_all_becomes_provider_unavailable(self):
        exc = http_retry.HTTPRetryError("connection refused")
        assert isinstance(router.classify_http_error(exc), router.ProviderUnavailable)


class TestResolvedChain:
    def test_default_provider_is_ollama_when_nothing_is_configured(self, org_id):
        assert router.resolved_chain(org_id) == [{"provider": "ollama", "model": ""}]

    def test_empty_chain_falls_back_to_the_single_active_provider(self, org_id):
        core_settings.update_settings(
            org_id, "llm", {"provider": "anthropic", "anthropic_model": "claude-opus"}
        )
        assert router.resolved_chain(org_id) == [
            {"provider": "anthropic", "model": "claude-opus"}
        ]

    def test_a_configured_chain_wins_over_the_single_provider_setting(self, org_id):
        core_settings.update_settings(org_id, "llm", {
            "provider": "ollama",
            "chain": [
                {"provider": "openai", "model": "gpt-4o"},
                {"provider": "anthropic", "model": "claude-opus"},
            ],
        })
        assert router.resolved_chain(org_id) == [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "anthropic", "model": "claude-opus"},
        ]

    def test_a_chain_survives_a_settings_round_trip(self, org_id):
        chain = [{"provider": "gemini", "model": "gemini-2.5-pro"}]
        core_settings.update_settings(org_id, "llm", {"chain": chain})
        assert core_settings.get_settings(org_id, "llm")["chain"] == chain
        assert router.resolved_chain(org_id) == chain


class TestExtractStructuredFallback:
    def test_provider_unavailable_rolls_down_to_the_next_chain_entry(self, org_id, monkeypatch):
        core_settings.update_settings(org_id, "llm", {"chain": [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "anthropic", "model": "claude-opus"},
        ]})
        monkeypatch.setattr(
            openai_llm, "call_structured",
            mock.Mock(side_effect=router.ProviderUnavailable("OPENAI_API_KEY not set")),
        )
        monkeypatch.setattr(anthropic_llm, "call_structured", mock.Mock(return_value={"x": "ok"}))

        assert router.extract_structured(org_id, "prompt", SCHEMA) == {"x": "ok"}

    def test_a_malformed_response_gets_one_same_provider_repair_retry_never_a_cascade(
        self, org_id, monkeypatch
    ):
        core_settings.update_settings(org_id, "llm", {"chain": [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "anthropic", "model": "claude-opus"},
        ]})
        prompts_seen = []

        def flaky(prompt, schema, *, model, settings, temperature=None, max_tokens=None):
            prompts_seen.append(prompt)
            raise ValueError("not valid json")

        monkeypatch.setattr(openai_llm, "call_structured", flaky)
        anthropic_mock = mock.Mock(return_value={"x": "should never be reached"})
        monkeypatch.setattr(anthropic_llm, "call_structured", anthropic_mock)

        with pytest.raises(router.LLMError):
            router.extract_structured(org_id, "prompt", SCHEMA)

        assert len(prompts_seen) == 2, "malformed output gets exactly one same-provider retry"
        assert "previous response could not be used" in prompts_seen[1]
        anthropic_mock.assert_not_called()

    def test_every_provider_unavailable_raises_one_summarizing_error(self, org_id, monkeypatch):
        core_settings.update_settings(org_id, "llm", {
            "chain": [{"provider": "openai", "model": "gpt-4o"}],
        })
        monkeypatch.setattr(
            openai_llm, "call_structured",
            mock.Mock(side_effect=router.ProviderUnavailable("OPENAI_API_KEY not set")),
        )
        with pytest.raises(router.LLMError) as exc_info:
            router.extract_structured(org_id, "prompt", SCHEMA)
        assert "openai" in str(exc_info.value)

    def test_the_validate_callback_runs_on_the_successful_result(self, org_id, monkeypatch):
        core_settings.update_settings(org_id, "llm", {
            "chain": [{"provider": "openai", "model": "gpt-4o"}],
        })
        monkeypatch.setattr(openai_llm, "call_structured", mock.Mock(return_value={"x": "raw"}))
        result = router.extract_structured(
            org_id, "prompt", SCHEMA, validate=lambda obj: {**obj, "validated": True},
        )
        assert result == {"x": "raw", "validated": True}


class TestCallLlm:
    def test_dispatches_to_the_configured_provider_and_model(self, org_id, monkeypatch):
        core_settings.update_settings(
            org_id, "llm", {"provider": "anthropic", "anthropic_model": "claude-opus"}
        )
        fake_call = mock.Mock(return_value="hello")
        monkeypatch.setattr(anthropic_llm, "call", fake_call)

        result = router.call_llm(org_id, "hi", want_json=True, temperature=0.7)

        assert result == "hello"
        _, kwargs = fake_call.call_args
        assert kwargs["model"] == "claude-opus"
        assert kwargs["want_json"] is True
        assert kwargs["temperature"] == 0.7

    def test_call_llm_does_not_cascade_to_another_provider_on_failure(self, org_id, monkeypatch):
        core_settings.update_settings(
            org_id, "llm", {"provider": "openai", "openai_model": "gpt-4o"}
        )
        monkeypatch.setattr(
            openai_llm, "call", mock.Mock(side_effect=router.ProviderUnavailable("down"))
        )
        with pytest.raises(router.ProviderUnavailable):
            router.call_llm(org_id, "hi")


class TestListModels:
    def test_delegates_to_the_adapter_with_the_full_settings_section(self, org_id, monkeypatch):
        core_settings.update_settings(org_id, "llm", {"ollama_host": "gpu-box"})
        fake = mock.Mock(return_value=["llama3", "mistral"])
        monkeypatch.setattr(ollama_llm, "list_models", fake)

        assert router.list_models(org_id, "ollama") == ["llama3", "mistral"]
        (settings_arg,), _ = fake.call_args
        assert settings_arg["ollama_host"] == "gpu-box"


def _patch_all_adapter_calls(monkeypatch, *, result="ok"):
    for adapter in (ollama_llm, openai_llm, anthropic_llm, gemini_llm, claudecode_llm):
        monkeypatch.setattr(adapter, "call", mock.Mock(return_value=result))


class TestCheckLlmStatus:
    def test_unconfigured_keyed_providers_report_not_configured_without_probing(
        self, org_id, monkeypatch
    ):
        probe = mock.Mock(return_value="ok")
        monkeypatch.setattr(openai_llm, "call", probe)
        monkeypatch.setattr(anthropic_llm, "call", probe)
        monkeypatch.setattr(gemini_llm, "call", probe)
        monkeypatch.setattr(claudecode_llm, "call", probe)
        monkeypatch.setattr(ollama_llm, "call", mock.Mock(return_value="ok"))

        result = router.check_llm_status(org_id)

        for provider in ("openai", "anthropic", "gemini", "claudecode"):
            assert result[provider] == {"ok": False, "error": "not configured"}
        probe.assert_not_called()
        assert result["ollama"] == {"ok": True, "error": None}

    def test_a_configured_provider_probe_reports_ok_on_success(self, org_id, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _patch_all_adapter_calls(monkeypatch)

        result = router.check_llm_status(org_id)

        assert result["openai"] == {"ok": True, "error": None}

    def test_a_configured_provider_probe_surfaces_the_error_on_failure(self, org_id, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        _patch_all_adapter_calls(monkeypatch)
        monkeypatch.setattr(
            anthropic_llm, "call", mock.Mock(side_effect=router.ProviderUnavailable("timeout"))
        )

        result = router.check_llm_status(org_id)

        assert result["anthropic"]["ok"] is False
        assert "timeout" in result["anthropic"]["error"]


class TestConnection:
    def test_requires_a_provider(self, org_id):
        with pytest.raises(router.LLMError):
            router.test_llm_connection(org_id, {})

    def test_uses_unsaved_overrides_and_never_touches_the_db(self, org_id, monkeypatch):
        fake = mock.Mock(return_value="ok")
        monkeypatch.setattr(openai_llm, "call", fake)

        result = router.test_llm_connection(
            org_id, {"provider": "openai", "openai_model": "gpt-4o-mini"}
        )

        assert result == {"ok": True, "error": None}
        assert core_settings.get_settings(org_id, "llm") == {}, (
            "test-connection must never persist overrides to core.settings"
        )


class TestOllamaAdapter:
    @pytest.fixture(autouse=True)
    def _clear_wake_state(self):
        ollama_llm._last_wake_failure.clear()
        yield
        ollama_llm._last_wake_failure.clear()

    def test_call_structured_sends_the_real_schema_as_format(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"message": {"content": '{"x": "y"}'}}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = ollama_llm.call_structured("extract", SCHEMA, model="llama3", settings={})

        assert result == {"x": "y"}
        assert captured["body"]["format"] == SCHEMA
        assert captured["body"]["model"] == "llama3"
        assert captured["url"].endswith("/api/chat")

    def test_call_only_sends_the_loose_json_hint_when_want_json_is_true(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"message": {"content": "plain text"}}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ollama_llm.call("hi", model="llama3", settings={}, want_json=False)

        assert "format" not in captured["body"]

    def test_list_models_hits_the_tags_endpoint(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            assert req.full_url.endswith("/api/tags")
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"models": [{"name": "llama3"}]}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            assert ollama_llm.list_models({}) == ["llama3"]

    def test_wake_on_lan_is_skipped_entirely_when_no_mac_is_configured(self, monkeypatch):
        monkeypatch.setattr(
            ollama_llm, "_is_reachable",
            mock.Mock(side_effect=AssertionError("must not check reachability without a mac")),
        )
        monkeypatch.setattr(
            ollama_llm, "_send_magic_packet",
            mock.Mock(side_effect=AssertionError("must not wake without a mac")),
        )

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"message": {"content": "hi"}}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ollama_llm.call("hi", model="llama3", settings={})

    def test_wake_on_lan_proceeds_once_the_host_becomes_reachable(self, monkeypatch):
        calls = {"n": 0}

        def fake_is_reachable(base_url):
            calls["n"] += 1
            return calls["n"] > 1

        monkeypatch.setattr(ollama_llm, "_is_reachable", fake_is_reachable)
        monkeypatch.setattr(ollama_llm, "_send_magic_packet", mock.Mock())
        monkeypatch.setattr(ollama_llm.time, "sleep", mock.Mock())

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"message": {"content": "hi"}}).encode(),
            )

        settings = {"ollama_mac": "AA:BB:CC:DD:EE:FF", "ollama_wake_timeout": 30}
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            assert ollama_llm.call("hi", model="llama3", settings=settings) == "hi"

    def test_wake_on_lan_raises_provider_unavailable_on_a_zero_timeout(self, monkeypatch):
        monkeypatch.setattr(ollama_llm, "_is_reachable", mock.Mock(return_value=False))
        monkeypatch.setattr(ollama_llm, "_send_magic_packet", mock.Mock())

        settings = {"ollama_mac": "AA:BB:CC:DD:EE:FF", "ollama_wake_timeout": 0}
        with pytest.raises(router.ProviderUnavailable):
            ollama_llm.call("hi", model="llama3", settings=settings)

    def test_wake_cooldown_prevents_hammering_a_recently_failed_host(self, monkeypatch):
        monkeypatch.setattr(ollama_llm, "_is_reachable", mock.Mock(return_value=False))
        send_packet = mock.Mock()
        monkeypatch.setattr(ollama_llm, "_send_magic_packet", send_packet)
        ollama_llm._last_wake_failure["AA:BB:CC:DD:EE:FF"] = ollama_llm.time.time()

        settings = {"ollama_mac": "AA:BB:CC:DD:EE:FF", "ollama_wake_timeout": 30}
        with pytest.raises(router.ProviderUnavailable):
            ollama_llm.call("hi", model="llama3", settings=settings)
        send_packet.assert_not_called()

    def test_wake_cooldown_expires_and_rechecks_rather_than_latching_forever(self, monkeypatch):
        """Safety rule 5 -- a host that was unreachable once must be re-checked,
        never permanently latched off."""
        monkeypatch.setattr(ollama_llm, "_is_reachable", mock.Mock(return_value=False))
        send_packet = mock.Mock()
        monkeypatch.setattr(ollama_llm, "_send_magic_packet", send_packet)
        ollama_llm._last_wake_failure["AA:BB:CC:DD:EE:FF"] = (
            ollama_llm.time.time() - ollama_llm._WAKE_RETRY_COOLDOWN_SECONDS - 1
        )

        settings = {"ollama_mac": "AA:BB:CC:DD:EE:FF", "ollama_wake_timeout": 0}
        with pytest.raises(router.ProviderUnavailable):
            ollama_llm.call("hi", model="llama3", settings=settings)
        send_packet.assert_called_once()


class TestOpenAIAdapter:
    def test_call_structured_uses_strict_mode_json_schema(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(
                    {"choices": [{"message": {"content": '{"x": "y"}'}}]}
                ).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = openai_llm.call_structured("extract", SCHEMA, model="gpt-4o", settings={})

        assert result == {"x": "y"}
        rf = captured["body"]["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["schema"] == SCHEMA
        assert captured["headers"].get("Authorization") == "Bearer sk-test"

    def test_missing_api_key_raises_provider_unavailable_without_a_network_call(
        self, monkeypatch
    ):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with mock.patch("urllib.request.urlopen") as urlopen:
            with pytest.raises(router.ProviderUnavailable):
                openai_llm.call("hi", model="gpt-4o", settings={})
        urlopen.assert_not_called()


class TestAnthropicAdapter:
    def test_call_structured_forces_tool_use(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({
                    "content": [{"type": "tool_use", "name": "extract", "input": {"x": "y"}}]
                }).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = anthropic_llm.call_structured(
                "extract", SCHEMA, model="claude-opus", settings={}
            )

        assert result == {"x": "y"}
        assert captured["body"]["tool_choice"] == {"type": "tool", "name": "extract"}
        assert captured["body"]["tools"][0]["input_schema"] == SCHEMA

    def test_no_tool_use_block_raises_value_error_not_provider_unavailable(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(
                    {"content": [{"type": "text", "text": "sorry, no"}]}
                ).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(ValueError):
                anthropic_llm.call_structured("extract", SCHEMA, model="claude-opus", settings={})


class TestGeminiAdapter:
    def test_call_structured_sets_response_schema_in_generation_config(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({
                    "candidates": [{"content": {"parts": [{"text": '{"x": "y"}'}]}}]
                }).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = gemini_llm.call_structured(
                "extract", SCHEMA, model="gemini-2.5-pro", settings={}
            )

        assert result == {"x": "y"}
        gen_cfg = captured["body"]["generationConfig"]
        assert gen_cfg["responseMimeType"] == "application/json"
        assert gen_cfg["responseSchema"] == SCHEMA
        assert "key=test-key" in captured["url"]

    def test_empty_candidates_raise_value_error_not_provider_unavailable(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"candidates": []}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(ValueError):
                gemini_llm.call("hi", model="gemini-2.5-pro", settings={})


class TestClaudeCodeAdapter:
    def test_call_structured_embeds_the_schema_as_prompt_text_and_extracts_json(
        self, monkeypatch
    ):
        responses = iter([
            {"requestId": "req-1"},
            {"status": "completed", "response": 'Sure, here you go: {"x": "y"} thanks'},
        ])
        captured = {}

        def fake_urlopen(req, timeout=None):
            if req.data is not None:
                captured["submit_body"] = json.loads(req.data.decode("utf-8"))
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(next(responses)).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("time.sleep"):
            result = claudecode_llm.call_structured(
                "extract", SCHEMA, model="sonnet", settings={}
            )

        assert result == {"x": "y"}
        assert json.dumps(SCHEMA) in captured["submit_body"]["message"]

    def test_no_json_object_in_the_response_raises_value_error(self, monkeypatch):
        responses = iter([
            {"requestId": "req-1"},
            {"status": "completed", "response": "no json here at all"},
        ])

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(next(responses)).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("time.sleep"):
            with pytest.raises(ValueError):
                claudecode_llm.call_structured("extract", SCHEMA, model="sonnet", settings={})

    def test_a_failed_status_raises_a_plain_llm_error(self, monkeypatch):
        responses = iter([
            {"requestId": "req-1"},
            {"status": "failed", "error": "bridge exploded"},
        ])

        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(next(responses)).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("time.sleep"):
            with pytest.raises(router.LLMError):
                claudecode_llm.call("hi", model="sonnet", settings={})

    def test_missing_key_still_sends_a_bearer_header_of_not_needed(self, monkeypatch):
        monkeypatch.delenv("CLAUDECODE_KEY", raising=False)
        responses = iter([{"requestId": "req-1"}, {"status": "completed", "response": "hi"}])
        headers_seen = []

        def fake_urlopen(req, timeout=None):
            headers_seen.append(dict(req.header_items()))
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps(next(responses)).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("time.sleep"):
            claudecode_llm.call("hi", model="sonnet", settings={})

        assert headers_seen[0].get("Authorization") == "Bearer not-needed"

    def test_poll_timeout_raises_provider_unavailable_not_a_plain_llm_error(self, monkeypatch):
        monkeypatch.setattr(claudecode_llm, "_POLL_TIMEOUT_SECONDS", 0)

        def fake_urlopen(req, timeout=None):
            if req.data is not None:
                return mock.Mock(
                    __enter__=lambda s: s, __exit__=lambda *a: None,
                    read=lambda: json.dumps({"requestId": "req-1"}).encode(),
                )
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: json.dumps({"status": "pending"}).encode(),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("time.sleep"):
            with pytest.raises(router.ProviderUnavailable):
                claudecode_llm.call("hi", model="sonnet", settings={})

    def test_list_models_returns_the_fixed_alias_list_with_no_network_call(self, monkeypatch):
        with mock.patch("urllib.request.urlopen") as urlopen:
            assert claudecode_llm.list_models({}) == list(claudecode_llm.MODEL_ALIASES)
        urlopen.assert_not_called()
