"""The generic REST router (`server/api/records.py`) and the error-mapping
handlers registered on the app (`server/api/app.py`).

Exercised at the HTTP layer via `TestClient` rather than by calling
`server.core.repository` directly -- the point of this file is that R4's
promise (one CRUD path reachable by every entity, core's and every module's,
with zero per-entity route code) actually holds over the wire, including the
error-status mapping that `records.py` itself deliberately does not implement.
"""
from __future__ import annotations

import pytest

from server.core import modules, registry, users


def _login(client, email="admin@example.com", password="correct-horse-battery"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


class TestEntityMetadata:
    def test_list_entities_only_includes_readable_ones(self, client, org_id, admin):
        _login(client)
        response = client.get("/records")
        assert response.status_code == 200
        names = {e["name"] for e in response.json()["entities"]}
        assert "organization" in names
        assert "person" in names
        assert "fund" in names  # modules/funds proves the module seam here too

    def test_list_entities_carries_nav_placement(self, client, org_id, admin):
        _login(client)
        entities = {e["name"]: e for e in client.get("/records").json()["entities"]}
        assert entities["organization"]["nav"] == "primary"
        assert entities["fund"]["nav_group"] == "Investing"
        assert entities["contact_channel"]["nav"] == "none"
        assert entities["pipeline"]["nav"] == "settings"  # admin's own view: derived

    def test_list_entities_excludes_admin_only_for_a_granted_non_admin(
        self, client, org_id, member
    ):
        """`list_records`/`get_record` hard-require admin on an `admin_only`
        entity regardless of any role grant -- an explicit read grant on
        `pipeline` must not make it appear here, or the sidebar dangles a
        link that always 403s."""
        role = users.create_role(org_id, "Reader")
        users.set_role_scope(org_id, str(role["id"]), "pipeline", read_level="all")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))

        _login(client, "member@example.com")
        names = {e["name"] for e in client.get("/records").json()["entities"]}
        assert "pipeline" not in names

    def test_schema_describes_fields_and_custom_fields(self, client, org_id, admin):
        _login(client)
        response = client.get("/records/organization/schema")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "organization"
        assert "name" in body["fields"]
        assert body["fields"]["name"]["required"] is True
        assert body["custom_fields"] == []

    def test_schema_for_unknown_entity_is_404(self, client, org_id, admin):
        _login(client)
        response = client.get("/records/not_a_real_entity/schema")
        assert response.status_code == 404

    def test_schema_is_reachable_by_a_granted_non_admin(self, client, org_id, member):
        """The schema endpoint checks `read` on the entity, not `is_admin` --
        a member with an explicit grant sees it despite never being admin."""
        role = users.create_role(org_id, "Reader")
        users.set_role_scope(org_id, str(role["id"]), "organization", read_level="all")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))

        _login(client, "member@example.com")
        response = client.get("/records/organization/schema")
        assert response.status_code == 200

    def test_schema_exposes_labels_references_and_operators(self, client, org_id, admin):
        """Phase 0's registry declarations (label/references/list_columns)
        must actually reach the wire -- this is what the frontend's filter
        builder and reference-field picker are driven from."""
        _login(client)
        response = client.get("/records/deal/schema")
        assert response.status_code == 200
        body = response.json()
        pipeline_field = body["fields"]["pipeline_id"]
        assert pipeline_field["references"] == "pipeline"
        assert pipeline_field["references_type_field"] is None
        assert pipeline_field["label"] == "Pipeline"
        assert "eq" in pipeline_field["operators"]
        assert body["list_columns"] == ["name", "stage", "amount", "status", "rotting"]

    def test_schema_exposes_polymorphic_references(self, client, org_id, admin):
        _login(client)
        response = client.get("/records/task/schema")
        assert response.status_code == 200
        subject_field = response.json()["fields"]["subject_id"]
        assert subject_field["references"] is None
        assert subject_field["references_type_field"] == "subject_type"

    def test_a_computed_unfilterable_field_has_no_operators(self, client, org_id, admin):
        """`deal.rotting` is a compute field, deliberately unfilterable
        (registry.verify() enforces this) -- the frontend's filter builder
        must not offer operators for a field the compiler would reject."""
        _login(client)
        response = client.get("/records/deal/schema")
        rotting_field = response.json()["fields"]["rotting"]
        assert rotting_field["filterable"] is False
        assert rotting_field["operators"] == []


class TestCrud:
    def test_create_list_get_update_delete(self, client, org_id, admin):
        _login(client)

        created = client.post("/records/organization", json={"name": "Acme Corp"})
        assert created.status_code == 201, created.text
        record = created.json()["record"]
        assert record["name"] == "Acme Corp"
        record_id = record["id"]

        listed = client.get("/records/organization")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        got = client.get(f"/records/organization/{record_id}")
        assert got.status_code == 200
        assert got.json()["record"]["name"] == "Acme Corp"

        updated = client.patch(
            f"/records/organization/{record_id}", json={"changes": {"name": "Acme Corporation"}}
        )
        assert updated.status_code == 200
        assert updated.json()["record"]["name"] == "Acme Corporation"

        deleted = client.delete(f"/records/organization/{record_id}")
        assert deleted.status_code == 204

        missing = client.get(f"/records/organization/{record_id}")
        assert missing.status_code == 404

    def test_get_missing_record_is_404(self, client, org_id, admin):
        _login(client)
        response = client.get(
            "/records/organization/00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404

    def test_create_missing_required_field_is_400(self, client, org_id, admin):
        _login(client)
        response = client.post("/records/organization", json={"domain": "acme.com"})
        assert response.status_code == 400

    def test_optimistic_concurrency_conflict_is_409(self, client, org_id, admin):
        _login(client)
        created = client.post("/records/organization", json={"name": "Acme"}).json()["record"]

        stale = client.patch(
            f"/records/organization/{created['id']}",
            json={
                "changes": {"name": "Too Late"},
                "if_unmodified_since": "2000-01-01T00:00:00+00:00",
            },
        )
        assert stale.status_code == 409

    def test_query_endpoint_matches_querystring_semantics(self, client, org_id, admin):
        _login(client)
        client.post("/records/organization", json={"name": "Acme"})
        client.post("/records/organization", json={"name": "Globex"})

        response = client.post(
            "/records/organization/query",
            json={"filters": {"field": "name", "op": "eq", "value": "Globex"}},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["records"][0]["name"] == "Globex"

    def test_module_entity_gets_the_same_generic_crud(self, client, org_id, admin):
        """R4's whole promise: `modules/funds`'s `fund` entity works through
        this router with zero fund-specific route code."""
        _login(client)
        created = client.post("/records/fund", json={"name": "Fund I"})
        assert created.status_code == 201, created.text
        assert created.json()["record"]["name"] == "Fund I"

    def test_saved_view_create_with_only_required_fields(self, client, org_id, admin):
        """core.saved_views' filters/sort/columns columns are NOT NULL with a
        jsonb DEFAULT -- that default only applies when the column is left
        out of the INSERT entirely. A client must omit the key, not send an
        explicit JSON null, to get it; this locks in that the omit-path
        works (the frontend's web/src/views/useSavedViews.ts relies on it)."""
        _login(client)
        created = client.post(
            "/records/saved_view", json={"entity": "organization", "name": "My view", "kind": "table"}
        )
        assert created.status_code == 201, created.text
        record = created.json()["record"]
        assert record["filters"] == []
        assert record["sort"] == []
        assert record["columns"] == []


class TestAssociations:
    def test_associate_end_and_delete(self, client, org_id, admin):
        _login(client)
        person = client.post("/records/person", json={"full_name": "Jane Doe"}).json()["record"]
        org = client.post("/records/organization", json={"name": "Acme"}).json()["record"]

        created = client.post(
            "/associations",
            json={
                "role": "works_at",
                "from_type": "person", "from_id": person["id"],
                "to_type": "organization", "to_id": org["id"],
            },
        )
        assert created.status_code == 201, created.text
        association = created.json()["association"]

        related = client.get(f"/records/person/{person['id']}/related")
        assert related.status_code == 200
        blocks = related.json()["related"]
        assert any(
            item["role"] == "works_at"
            for items in blocks.values()
            for item in items
        )

        ended = client.post(f"/associations/{association['id']}/end", json={})
        assert ended.status_code == 200
        assert ended.json()["association"]["valid_to"] is not None

        deleted = client.delete(f"/associations/{association['id']}")
        assert deleted.status_code == 204

    def test_unknown_role_is_400(self, client, org_id, admin):
        _login(client)
        person = client.post("/records/person", json={"full_name": "Jane Doe"}).json()["record"]
        org = client.post("/records/organization", json={"name": "Acme"}).json()["record"]

        response = client.post(
            "/associations",
            json={
                "role": "not_a_real_role",
                "from_type": "person", "from_id": person["id"],
                "to_type": "organization", "to_id": org["id"],
            },
        )
        assert response.status_code == 400


class TestPermissions:
    def test_unauthenticated_is_401(self, client, org_id, admin):
        response = client.get("/records/organization")
        assert response.status_code == 401

    def test_admin_only_entity_is_403_for_a_member(self, client, org_id, member):
        _login(client, "member@example.com")
        response = client.get("/records/pipeline")
        assert response.status_code == 403

    def test_admin_only_entity_get_by_id_is_403_for_a_member(self, client, org_id, admin, member):
        """Regression: `get_record` used to skip the `admin_only` guard the
        other four CRUD paths already applied, so a member who somehow had a
        record id could fetch it directly even though `GET /records/pipeline`
        (list) correctly 403s them."""
        _login(client)
        pipeline = client.post(
            "/records/pipeline", json={"name": "Standard Pipeline"}
        ).json()["record"]

        _login(client, "member@example.com")
        response = client.get(f"/records/pipeline/{pipeline['id']}")
        assert response.status_code == 403

    def test_bad_filter_syntax_is_400(self, client, org_id, admin):
        _login(client)
        response = client.get(
            "/records/organization", params={"filter": json_bad_op()}
        )
        assert response.status_code == 400

    def test_malformed_json_filter_is_400(self, client, org_id, admin):
        _login(client)
        response = client.get(
            "/records/organization", params={"filter": "{not json"}
        )
        assert response.status_code == 400


class TestEntityRoles:
    def test_lists_roles_from_both_sides(self, client, org_id, admin):
        _login(client)
        response = client.get("/records/person/roles")
        assert response.status_code == 200
        roles = {(r["role"], r["direction"]) for r in response.json()["roles"]}
        assert ("works_at", "from") in roles  # person is the from-side
        # advisor_to's inverse label applies to person as a to-side too
        assert any(r == "advisor_to" and d == "to" for r, d in roles)

    def test_roles_carry_group_and_label(self, client, org_id, admin):
        _login(client)
        roles = client.get("/records/person/roles").json()["roles"]
        works_at = next(r for r in roles if r["role"] == "works_at" and r["direction"] == "from")
        assert works_at["group"] == "People"
        assert works_at["group_order"] == 30
        assert works_at["label"] == "Works at"
        assert works_at["hierarchical"] is False

    def test_unknown_entity_is_404(self, client, org_id, admin):
        _login(client)
        response = client.get("/records/not_a_real_entity/roles")
        assert response.status_code == 404


class TestAppBootstrapsItsOwnRegistry:
    def test_lifespan_populates_the_registry_without_conftest_help(self):
        """Regression: server/api/app.py's lifespan must call the module
        loader itself. This is easy to lose silently, because
        tests/conftest.py's session-scoped `_migrated` fixture ALSO
        populates the registry before any `client` fixture ever runs --
        which would mask a real deployment's lifespan doing nothing at all,
        exactly the bug this test catches (found by driving the actual app
        in a browser, where nothing plays conftest's role)."""
        from fastapi.testclient import TestClient

        from server.api.app import app

        registry.reset()
        try:
            assert registry.entities() == []
            with TestClient(app):
                pass  # lifespan runs on enter/exit
            assert "organization" in registry.entities()
            assert "fund" in registry.entities()  # a module's entity, not just core's
        finally:
            # Restore what conftest's session fixture set up, so every test
            # after this one still sees a populated registry.
            registry.reset()
            modules.install_enabled_modules()


def json_bad_op() -> str:
    import json

    return json.dumps({"field": "name", "op": "not_a_real_operator", "value": "x"})
