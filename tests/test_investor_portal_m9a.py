"""M9a: investor classification -- categories, pathways, questionnaires,
responses, and the mandate derived from them.

Exercised through `server.core.repository` directly (like
`test_vertical_funds.py`), not the HTTP layer -- the point here is the module
seam and the derivation logic, not the REST router (already proven generic
in `tests/test_records_api.py`).
"""
from __future__ import annotations

import pytest

from server.core import registry, repository, users


@pytest.fixture
def fund_i(principal):
    return repository.create(principal, "fund", {"name": "Fund I"})


@pytest.fixture
def jane(principal):
    return repository.create(principal, "person", {"full_name": "Jane Doe"})


class TestOrgSeeding:
    def test_creating_an_org_seeds_six_categories_two_enabled(self, org_id, principal):
        result = repository.list_records(principal, "investor_category")
        assert result["total"] == 6

        by_key = {r["key"]: r for r in result["records"]}
        assert set(by_key) == {
            "accredited_individual", "accredited_entity", "qualified_purchaser",
            "qualified_client", "institutional", "non_accredited",
        }
        assert by_key["accredited_individual"]["is_enabled"] is True
        assert by_key["accredited_entity"]["is_enabled"] is True
        assert by_key["qualified_purchaser"]["is_enabled"] is False
        assert by_key["qualified_purchaser"]["requires_verification"] is True

    def test_seeding_is_scoped_per_org(self, principal):
        """A second org must get its own six rows, not see -- or duplicate
        into -- the first org's."""
        other_org = users.create_org("Other Org", "other-org")
        other_admin = users.create_user(
            other_org["id"], email="admin2@example.com", name="Admin Two",
            password="correct-horse-battery", is_admin=True,
        )
        from server.core import permissions
        other_principal = permissions.load_principal(other_admin)

        first = repository.list_records(principal, "investor_category")
        second = repository.list_records(other_principal, "investor_category")
        assert first["total"] == 6
        assert second["total"] == 6
        assert {r["id"] for r in first["records"]}.isdisjoint(
            {r["id"] for r in second["records"]}
        )


class TestReferenceDataCrud:
    def test_investment_pathway_crud(self, principal):
        created = repository.create(
            principal, "investment_pathway",
            {"key": "network", "label": "Investor Network", "default_min_check": 5000},
        )
        assert created["label"] == "Investor Network"
        updated = repository.update(
            principal, "investment_pathway", str(created["id"]), {"is_enabled": False}
        )
        assert updated["is_enabled"] is False

    def test_pathway_vehicle_links_a_pathway_to_a_real_fund(self, principal, fund_i):
        pathway = repository.create(
            principal, "investment_pathway", {"key": "flagship", "label": "Flagship Fund"}
        )
        vehicle = repository.create(
            principal, "pathway_vehicle",
            {"pathway_id": str(pathway["id"]), "fund_id": str(fund_i["id"])},
        )
        assert vehicle["fund_id"] == str(fund_i["id"])

    def test_investor_profile_tracks_status_and_relationship(self, principal, jane):
        profile = repository.create(
            principal, "investor_profile",
            {
                "subject_type": "person", "subject_id": str(jane["id"]),
                "status": "prospect", "relationship_since": "2024-01-15",
            },
        )
        assert profile["status"] == "prospect"
        assert profile["accreditation_method"] == "self_certified"  # DB default

        updated = repository.update(
            principal, "investor_profile", str(profile["id"]), {"status": "active"}
        )
        assert updated["status"] == "active"

    def test_investor_profile_is_unique_per_subject(self, principal, jane):
        repository.create(
            principal, "investor_profile",
            {"subject_type": "person", "subject_id": str(jane["id"])},
        )
        with pytest.raises(Exception):
            repository.create(
                principal, "investor_profile",
                {"subject_type": "person", "subject_id": str(jane["id"])},
            )


class TestQuestionnaireVersioning:
    def test_publishing_v2_does_not_change_what_a_v1_response_meant(self, principal, jane):
        v1 = repository.create(
            principal, "questionnaire",
            {
                "name": "Intake", "version": 1,
                "question_schema": [
                    {"key": "check_size", "type": "number", "maps_to": "check_min"},
                ],
            },
        )
        response = repository.create(
            principal, "questionnaire_response",
            {
                "questionnaire_id": str(v1["id"]), "questionnaire_version": 1,
                "subject_type": "person", "subject_id": str(jane["id"]),
                "answers": {"check_size": 25000},
            },
        )
        assert response["questionnaire_version"] == 1

        v2 = repository.create(
            principal, "questionnaire",
            {
                "name": "Intake", "version": 2,
                "question_schema": [
                    {"key": "check_size_v2", "type": "number", "maps_to": "check_min"},
                ],
            },
        )
        assert v2["version"] == 2

        refetched = repository.get_record(principal, "questionnaire_response", str(response["id"]))
        assert refetched["questionnaire_version"] == 1
        assert refetched["questionnaire_id"] == str(v1["id"])


class TestMandateDerivation:
    def test_a_response_derives_a_mandate(self, principal, jane):
        questionnaire = repository.create(
            principal, "questionnaire",
            {
                "name": "Intake", "version": 1,
                "question_schema": [
                    {"key": "classes", "type": "multiselect", "maps_to": "asset_classes"},
                    {"key": "min_check", "type": "number", "maps_to": "check_min"},
                    {"key": "notes", "type": "text"},  # no maps_to -- informational only
                ],
            },
        )
        repository.create(
            principal, "questionnaire_response",
            {
                "questionnaire_id": str(questionnaire["id"]), "questionnaire_version": 1,
                "subject_type": "person", "subject_id": str(jane["id"]),
                "answers": {"classes": ["venture", "growth_equity"], "min_check": 25000,
                            "notes": "met at a conference"},
            },
        )

        result = repository.list_records(
            principal, "investor_mandate",
            filters={"and": [
                {"field": "subject_type", "op": "eq", "value": "person"},
                {"field": "subject_id", "op": "eq", "value": str(jane["id"])},
            ]},
        )
        assert result["total"] == 1
        mandate = result["records"][0]
        assert mandate["asset_classes"] == ["venture", "growth_equity"]
        assert float(mandate["check_min"]) == 25000
        assert mandate["source"] == "response"
        assert mandate["derived_from_response_id"] is not None

    def test_a_skipped_question_leaves_the_mandate_field_null(self, principal, jane):
        """Null fails safe -- an unanswered question must not default to
        'matches everything.'"""
        questionnaire = repository.create(
            principal, "questionnaire",
            {
                "name": "Intake", "version": 1,
                "question_schema": [
                    {"key": "classes", "type": "multiselect", "maps_to": "asset_classes"},
                    {"key": "min_check", "type": "number", "maps_to": "check_min"},
                ],
            },
        )
        repository.create(
            principal, "questionnaire_response",
            {
                "questionnaire_id": str(questionnaire["id"]), "questionnaire_version": 1,
                "subject_type": "person", "subject_id": str(jane["id"]),
                "answers": {"classes": ["venture"]},  # min_check skipped entirely
            },
        )
        mandate = repository.list_records(
            principal, "investor_mandate",
            filters={"field": "subject_id", "op": "eq", "value": str(jane["id"])},
        )["records"][0]
        assert mandate["check_min"] is None

    def test_a_second_response_updates_the_same_mandate_in_place(self, principal, jane):
        questionnaire = repository.create(
            principal, "questionnaire",
            {
                "name": "Intake", "version": 1,
                "question_schema": [
                    {"key": "min_check", "type": "number", "maps_to": "check_min"},
                ],
            },
        )
        first = repository.create(
            principal, "questionnaire_response",
            {
                "questionnaire_id": str(questionnaire["id"]), "questionnaire_version": 1,
                "subject_type": "person", "subject_id": str(jane["id"]),
                "answers": {"min_check": 10000},
            },
        )
        second = repository.create(
            principal, "questionnaire_response",
            {
                "questionnaire_id": str(questionnaire["id"]), "questionnaire_version": 1,
                "subject_type": "person", "subject_id": str(jane["id"]),
                "answers": {"min_check": 50000},
            },
        )

        result = repository.list_records(
            principal, "investor_mandate",
            filters={"field": "subject_id", "op": "eq", "value": str(jane["id"])},
        )
        assert result["total"] == 1  # updated in place, not a second row
        mandate = result["records"][0]
        assert float(mandate["check_min"]) == 50000
        assert mandate["derived_from_response_id"] == str(second["id"])
        assert mandate["derived_from_response_id"] != str(first["id"])

    def test_a_mandate_can_be_corrected_by_hand(self, principal, jane):
        """The 'corrected by hand' bar M9a is done when it clears -- a human
        edit through the ordinary generic CRUD path, no special API."""
        questionnaire = repository.create(
            principal, "questionnaire",
            {"name": "Intake", "version": 1, "question_schema": [
                {"key": "min_check", "type": "number", "maps_to": "check_min"},
            ]},
        )
        repository.create(
            principal, "questionnaire_response",
            {
                "questionnaire_id": str(questionnaire["id"]), "questionnaire_version": 1,
                "subject_type": "person", "subject_id": str(jane["id"]),
                "answers": {"min_check": 10000},
            },
        )
        mandate = repository.list_records(
            principal, "investor_mandate",
            filters={"field": "subject_id", "op": "eq", "value": str(jane["id"])},
        )["records"][0]

        corrected = repository.update(
            principal, "investor_mandate", str(mandate["id"]),
            {"check_min": 15000, "source": "human", "reviewed_by": None},
        )
        assert float(corrected["check_min"]) == 15000
        assert corrected["source"] == "human"


class TestRegistry:
    def test_every_m9a_entity_is_registered(self):
        for name in (
            "investor_category", "investment_pathway", "pathway_vehicle",
            "investor_profile", "questionnaire", "questionnaire_response",
            "investor_mandate",
        ):
            assert name in registry.entities()
            assert registry.entity(name).module == "investor_portal"
