"""The one shared HTTP retry implementation (server/net/http_retry.py).
No real network call anywhere -- urllib.request.urlopen is mocked.

Moved here from tests/test_esign.py once the retry mechanics moved out of
server/providers/esign.py into this shared module (M3): this is testing
the transport mechanics every axis's dispatcher builds on, not anything
e-signature-specific. See tests/test_esign.py's TestRetryHelper (the
409->NotReadyError translation, which stays esign-specific) for the
adapter-level test that remains there.
"""
from __future__ import annotations

import io
import urllib.error
from unittest import mock

import pytest

from server.net import http_retry


class TestRetryHelper:
    def test_retries_on_429_then_succeeds(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "rate limited", {"Retry-After": "0"},
                    io.BytesIO(b"{}"),
                )
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: b'{"ok": true}',
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = http_retry.request_with_retry(
                "GET", "https://example.test/x", headers={}, max_retries=3,
            )
        assert result == {"ok": True}
        assert calls["n"] == 2

    def test_a_non_retryable_status_raises_immediately_with_the_status_attached(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 409, "not ready", {}, io.BytesIO(b"{}")
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(http_retry.HTTPRetryError) as exc_info:
                http_retry.request_with_retry("GET", "https://example.test/x", headers={})
        assert calls["n"] == 1, "a non-retryable status must not be retried"
        assert exc_info.value.status_code == 409

    def test_401_raises_immediately(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 401, "unauthorized", {}, io.BytesIO(b"{}")
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(http_retry.HTTPRetryError) as exc_info:
                http_retry.request_with_retry("GET", "https://example.test/x", headers={})
        assert calls["n"] == 1
        assert exc_info.value.status_code == 401

    def test_a_connection_error_has_no_status_code(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(http_retry.HTTPRetryError) as exc_info:
                http_retry.request_with_retry(
                    "GET", "https://example.test/x", headers={}, max_retries=1,
                )
        assert exc_info.value.status_code is None

    def test_raw_returns_bytes_not_parsed_json(self):
        def fake_urlopen(req, timeout=None):
            return mock.Mock(
                __enter__=lambda s: s, __exit__=lambda *a: None,
                read=lambda: b"%PDF-1.4 not real bytes",
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = http_retry.request_with_retry(
                "GET", "https://example.test/x", headers={}, raw=True,
            )
        assert result == b"%PDF-1.4 not real bytes"

    def test_exhausting_retries_on_a_retryable_status_still_raises(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 503, "unavailable", {}, io.BytesIO(b"{}")
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("time.sleep"):
            with pytest.raises(http_retry.HTTPRetryError) as exc_info:
                http_retry.request_with_retry(
                    "GET", "https://example.test/x", headers={}, max_retries=3,
                )
        assert calls["n"] == 3
        assert exc_info.value.status_code == 503
