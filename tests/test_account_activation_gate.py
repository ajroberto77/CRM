"""Phase D: an investment_account cannot reach 'active' without its
required AML/KYC and tax-withholding documents on file, executed and
unexpired -- modules/investor_portal's second write-time gate, alongside
M9d's commitment-closing gate.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from server.core import repository, settings


@pytest.fixture
def us_person_account(principal):
    person = repository.create(
        principal, "person", {"full_name": "Jane Doe", "tax_residence_country": "US"}
    )
    return repository.create(
        principal, "investment_account",
        {"name": "Jane Doe -- Individual", "person_id": str(person["id"]),
         "account_type": "individual"},
    )


@pytest.fixture
def non_us_entity_account(principal):
    entity = repository.create(principal, "organization", {"name": "Doe Family Trust LLC"})
    return repository.create(
        principal, "investment_account",
        {"name": "Doe Family Trust", "entity_org_id": str(entity["id"]),
         "account_type": "trust", "domicile_country": "GB"},
    )


def _doc(principal, account, *, kind, status="executed", valid_until=None):
    values = {
        "subject_type": "investment_account", "subject_id": str(account["id"]),
        "kind": kind, "filename": f"{kind}.pdf", "status": status,
    }
    if valid_until is not None:
        values["valid_until"] = valid_until
    return repository.create(principal, "document", values)


class TestRequiredFormByCountry:
    def test_a_us_person_needs_aml_kyc_and_w9(self, principal, us_person_account):
        with pytest.raises(repository.ValidationError):
            repository.update(
                principal, "investment_account", str(us_person_account["id"]),
                {"status": "active"},
            )
        _doc(principal, us_person_account, kind="aml_kyc")
        with pytest.raises(repository.ValidationError):
            repository.update(
                principal, "investment_account", str(us_person_account["id"]),
                {"status": "active"},
            )
        _doc(principal, us_person_account, kind="w9")
        activated = repository.update(
            principal, "investment_account", str(us_person_account["id"]),
            {"status": "active"},
        )
        assert activated["status"] == "active"

    def test_a_non_us_entity_needs_w8bene_not_w8ben(self, principal, non_us_entity_account):
        _doc(principal, non_us_entity_account, kind="aml_kyc")
        _doc(principal, non_us_entity_account, kind="w8ben")
        with pytest.raises(repository.ValidationError):
            repository.update(
                principal, "investment_account", str(non_us_entity_account["id"]),
                {"status": "active"},
            )
        _doc(principal, non_us_entity_account, kind="w8bene")
        activated = repository.update(
            principal, "investment_account", str(non_us_entity_account["id"]),
            {"status": "active"},
        )
        assert activated["status"] == "active"

    def test_a_non_us_individual_needs_w8ben_not_w8bene(self, principal):
        person = repository.create(
            principal, "person", {"full_name": "Pierre Dubois", "tax_residence_country": "FR"}
        )
        account = repository.create(
            principal, "investment_account",
            {"name": "Pierre Dubois -- Individual", "person_id": str(person["id"]),
             "account_type": "individual"},
        )
        _doc(principal, account, kind="aml_kyc")
        _doc(principal, account, kind="w8bene")
        with pytest.raises(repository.ValidationError):
            repository.update(principal, "investment_account", str(account["id"]),
                              {"status": "active"})
        _doc(principal, account, kind="w8ben")
        activated = repository.update(principal, "investment_account", str(account["id"]),
                                      {"status": "active"})
        assert activated["status"] == "active"

    def test_no_country_on_file_blocks_activation(self, principal):
        """A null category never satisfies a gate -- an account with no
        country on file at all must not be activatable just because it has
        no specific form requirement to fail."""
        account = repository.create(
            principal, "investment_account", {"name": "No Country On File"}
        )
        _doc(principal, account, kind="aml_kyc")
        with pytest.raises(repository.ValidationError):
            repository.update(principal, "investment_account", str(account["id"]),
                              {"status": "active"})


class TestExpiry:
    def test_an_expired_document_does_not_satisfy_the_gate(self, principal, us_person_account):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _doc(principal, us_person_account, kind="aml_kyc", valid_until=yesterday)
        _doc(principal, us_person_account, kind="w9")
        with pytest.raises(repository.ValidationError):
            repository.update(principal, "investment_account", str(us_person_account["id"]),
                              {"status": "active"})

    def test_a_re_executed_document_unblocks_it(self, principal, us_person_account):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _doc(principal, us_person_account, kind="aml_kyc", valid_until=yesterday)
        _doc(principal, us_person_account, kind="w9")
        with pytest.raises(repository.ValidationError):
            repository.update(principal, "investment_account", str(us_person_account["id"]),
                              {"status": "active"})

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        _doc(principal, us_person_account, kind="aml_kyc", valid_until=tomorrow)
        activated = repository.update(
            principal, "investment_account", str(us_person_account["id"]),
            {"status": "active"},
        )
        assert activated["status"] == "active"

    def test_no_expiry_date_never_expires(self, principal, us_person_account):
        _doc(principal, us_person_account, kind="aml_kyc")
        _doc(principal, us_person_account, kind="w9")
        activated = repository.update(
            principal, "investment_account", str(us_person_account["id"]),
            {"status": "active"},
        )
        assert activated["status"] == "active"


class TestTransitionScope:
    def test_editing_an_already_active_account_does_not_recheck(self, principal, us_person_account):
        _doc(principal, us_person_account, kind="aml_kyc")
        _doc(principal, us_person_account, kind="w9")
        repository.update(principal, "investment_account", str(us_person_account["id"]),
                          {"status": "active"})

        # No new documents; an unrelated edit while already active must not
        # be blocked by re-checking a gate that only governs the transition.
        updated = repository.update(
            principal, "investment_account", str(us_person_account["id"]),
            {"base_currency": "GBP"},
        )
        assert updated["base_currency"] == "GBP"

    def test_moving_between_two_non_active_statuses_is_untouched(self, principal, us_person_account):
        updated = repository.update(
            principal, "investment_account", str(us_person_account["id"]),
            {"status": "onboarding"},
        )
        assert updated["status"] == "onboarding"

    def test_a_direct_create_as_active_is_refused(self, principal):
        """Creating an account already 'active' in one write is covered by
        the same gate, not only the update-transition case -- and can never
        actually satisfy it, since a compliance document can't reference an
        account id that doesn't exist yet."""
        person = repository.create(
            principal, "person", {"full_name": "Fresh Import", "tax_residence_country": "US"}
        )
        with pytest.raises(repository.ValidationError):
            repository.create(
                principal, "investment_account",
                {"name": "Fresh Import -- Individual", "person_id": str(person["id"]),
                 "account_type": "individual", "status": "active"},
            )


class TestGateStrictnessSetting:
    def test_the_gate_can_be_turned_off_per_org(self, principal, org_id, us_person_account):
        settings.update_settings(
            org_id, "compliance", {"enforce_account_activation_gate": False}
        )
        activated = repository.update(
            principal, "investment_account", str(us_person_account["id"]),
            {"status": "active"},
        )
        assert activated["status"] == "active"

    def test_the_gate_defaults_to_enforcing(self, principal, us_person_account):
        assert settings.get_settings(principal.org_id, "compliance") == {}
        with pytest.raises(repository.ValidationError):
            repository.update(
                principal, "investment_account", str(us_person_account["id"]),
                {"status": "active"},
            )
