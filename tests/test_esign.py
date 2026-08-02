"""The e-signature dispatcher (R3's sixth axis) and the document/signer
plumbing it feeds. No real vendor call is made anywhere in this file --
urllib is mocked, matching Cal's own testing convention for provider modules.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from server.core import documents as doc_signers
from server.core import repository
from server.providers import esign


class TestDispatch:
    def test_unknown_provider_raises(self):
        with pytest.raises(esign.EsignError):
            esign.create_and_send(
                "fax", subject="x", message="y", document_bytes=b"", filename="a.pdf",
                mime="application/pdf", signers=[esign.SignerSpec("A", "a@example.com")],
            )

    def test_at_least_one_signer_is_required(self):
        with pytest.raises(esign.EsignError):
            esign.create_and_send(
                "docusign", subject="x", message="y", document_bytes=b"",
                filename="a.pdf", mime="application/pdf", signers=[],
            )

    @pytest.mark.parametrize("provider", esign.PROVIDERS)
    def test_every_provider_fails_loudly_when_unconfigured(self, provider):
        """Never silently 'skip this provider' -- an org that thinks
        e-signature is configured must find out immediately."""
        with pytest.raises(esign.EsignDisabled):
            esign.create_and_send(
                provider, subject="x", message="y", document_bytes=b"x",
                filename="a.pdf", mime="application/pdf",
                signers=[esign.SignerSpec("A", "a@example.com")],
            )

    def test_an_adapter_returning_an_unnormalized_status_is_rejected(self, monkeypatch):
        """check_status()'s contract requires the adapter to return a status
        from esign.STATUSES -- an adapter bug here must not silently write a
        bogus value into core.documents.status, which has a CHECK constraint."""
        import server.providers.docusign_esign as ds

        monkeypatch.setattr(
            ds, "check_status",
            lambda envelope_id: esign.EnvelopeStatus(status="not_a_real_status"),
        )
        with pytest.raises(esign.EsignError):
            esign.check_status("docusign", "env-123")


class TestRetryHelper:
    """`_request_with_retry` is now a thin wrapper over
    server/net/http_retry.py's shared transport mechanics -- see
    tests/test_http_retry.py for retry/backoff/status-code coverage. This
    class only covers the one thing that stays esign-specific: translating
    a bare 409 into NotReadyError."""

    def test_409_raises_not_ready_without_retrying(self):
        import io
        import urllib.error

        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 409, "not ready", {}, io.BytesIO(b"{}")
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(esign.NotReadyError):
                esign._request_with_retry("GET", "https://example.test/x", headers={})
        assert calls["n"] == 1, "409 must not be retried -- it means 'not yet', not 'transient'"

    def test_a_non_409_failure_becomes_a_plain_esign_error(self):
        import io
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 401, "unauthorized", {}, io.BytesIO(b"{}")
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(esign.EsignError):
                esign._request_with_retry("GET", "https://example.test/x", headers={})


class TestDocuSignJwt:
    def test_jwt_is_correctly_rs256_signed(self):
        """The one piece of DocuSign's adapter that is fully verifiable
        without a live account: the JWT construction and RSA signature."""
        import base64

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        import server.providers.docusign_esign as ds

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        token = ds._sign_jwt("integration-key", "user-id", pem)
        header_b64, payload_b64, sig_b64 = token.split(".")

        def _pad(s: str) -> str:
            return s + "=" * (-len(s) % 4)

        header = json.loads(base64.urlsafe_b64decode(_pad(header_b64)))
        payload = json.loads(base64.urlsafe_b64decode(_pad(payload_b64)))
        assert header == {"alg": "RS256", "typ": "JWT"}
        assert payload["iss"] == "integration-key"
        assert payload["sub"] == "user-id"
        assert payload["scope"] == "signature impersonation"

        signing_input = (header_b64 + "." + payload_b64).encode()
        signature = base64.urlsafe_b64decode(_pad(sig_b64))
        key.public_key().verify(  # raises InvalidSignature if this is wrong
            signature, signing_input, padding.PKCS1v15(), hashes.SHA256(),
        )

    def test_a_malformed_pem_raises_a_clear_error_not_a_stack_trace(self):
        import server.providers.docusign_esign as ds
        from server.providers.esign import EsignDisabled

        with pytest.raises(EsignDisabled):
            ds._load_pem("not a real pem key")


class TestDocumentSigners:
    def test_signers_round_trip_and_all_signed_requires_at_least_one(self, principal):
        document = repository.create(
            principal, "document",
            {"subject_type": "commitment", "subject_id": "00000000-0000-0000-0000-000000000001",
             "kind": "subscription_agreement", "filename": "sub.pdf"},
        )
        assert doc_signers.all_signed(principal, str(document["id"])) is False

        signer = doc_signers.add_signer(
            principal, str(document["id"]),
            subject_type="person", subject_id="00000000-0000-0000-0000-000000000002",
            role="investor",
        )
        assert doc_signers.all_signed(principal, str(document["id"])) is False

        doc_signers.update_signer_status(principal, str(signer["id"]), "signed")
        assert doc_signers.all_signed(principal, str(document["id"])) is True

    def test_unknown_status_is_rejected(self, principal):
        document = repository.create(
            principal, "document",
            {"subject_type": "commitment", "subject_id": "00000000-0000-0000-0000-000000000001",
             "kind": "subscription_agreement", "filename": "sub.pdf"},
        )
        signer = doc_signers.add_signer(
            principal, str(document["id"]),
            subject_type="person", subject_id="00000000-0000-0000-0000-000000000002",
            role="investor",
        )
        with pytest.raises(ValueError):
            doc_signers.update_signer_status(principal, str(signer["id"]), "raptured")


class TestDocumentEntity:
    def test_document_is_a_registered_entity_with_generic_crud(self, principal):
        created = repository.create(
            principal, "document",
            {"subject_type": "organization", "subject_id": "00000000-0000-0000-0000-000000000003",
             "kind": "nda", "filename": "nda.pdf", "status": "draft"},
        )
        assert created["status"] == "draft"

        updated = repository.update(
            principal, "document", str(created["id"]), {"status": "executed"}
        )
        assert updated["status"] == "executed"

    def test_filtering_by_subject_and_kind(self, principal):
        repository.create(
            principal, "document",
            {"subject_type": "commitment", "subject_id": "00000000-0000-0000-0000-000000000004",
             "kind": "subscription_agreement", "filename": "a.pdf"},
        )
        repository.create(
            principal, "document",
            {"subject_type": "commitment", "subject_id": "00000000-0000-0000-0000-000000000005",
             "kind": "side_letter", "filename": "b.pdf"},
        )
        result = repository.list_records(
            principal, "document",
            filters={"field": "kind", "op": "eq", "value": "subscription_agreement"},
        )
        assert result["total"] == 1
        assert result["records"][0]["filename"] == "a.pdf"

    def test_storage_key_and_bytes_are_not_user_writable(self, principal):
        """These are set by whatever actually stores the file, not by request
        input -- writable=False on both."""
        with pytest.raises(Exception):
            repository.create(
                principal, "document",
                {"subject_type": "note", "subject_id": "00000000-0000-0000-0000-000000000006",
                 "filename": "x.pdf", "storage_key": "s3://attacker-controlled/x"},
            )
