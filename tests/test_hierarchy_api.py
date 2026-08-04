"""`GET /records/{entity}/{id}/hierarchy` (server/api/records.py,
`associations.hierarchy_chain()`) -- wraps `hierarchy.ancestors_of`/
`descendants_of`, hydrated the same way `related_blocks` hydrates ordinary
association edges: a step the principal cannot see is absent, not a 404 or
an error.
"""
from __future__ import annotations

from server.core import users


def _login(client, email="admin@example.com", password="correct-horse-battery"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


def _org(client, name):
    return client.post("/records/organization", json={"name": name}).json()["record"]


def _own(client, child, parent):
    response = client.post(
        "/associations",
        json={
            "role": "owned_by", "from_type": "organization", "from_id": child["id"],
            "to_type": "organization", "to_id": parent["id"],
        },
    )
    assert response.status_code == 201, response.text


class TestAncestorsAndDescendants:
    def test_walks_the_ownership_chain_nearest_first(self, client, org_id, admin):
        _login(client)
        grandchild = _org(client, "Grandchild Co")
        child = _org(client, "Child Co")
        parent = _org(client, "Ultimate Parent Co")
        _own(client, grandchild, child)
        _own(client, child, parent)

        response = client.get(
            f"/records/organization/{grandchild['id']}/hierarchy",
            params={"role": "owned_by"},
        )
        assert response.status_code == 200
        chain = response.json()["chain"]
        assert [step["record"]["name"] for step in chain] == ["Child Co", "Ultimate Parent Co"]
        assert [step["depth"] for step in chain] == [1, 2]

    def test_descendants_walks_the_same_chain_downward(self, client, org_id, admin):
        _login(client)
        grandchild = _org(client, "Grandchild Co")
        child = _org(client, "Child Co")
        parent = _org(client, "Ultimate Parent Co")
        _own(client, grandchild, child)
        _own(client, child, parent)

        response = client.get(
            f"/records/organization/{parent['id']}/hierarchy",
            params={"role": "owned_by", "direction": "descendants"},
        )
        assert response.status_code == 200
        chain = response.json()["chain"]
        assert [step["record"]["name"] for step in chain] == ["Child Co", "Grandchild Co"]

    def test_a_record_with_no_parent_has_an_empty_chain(self, client, org_id, admin):
        _login(client)
        lone = _org(client, "No Parent Co")
        response = client.get(
            f"/records/organization/{lone['id']}/hierarchy", params={"role": "owned_by"}
        )
        assert response.status_code == 200
        assert response.json()["chain"] == []

    def test_entity_id_type_is_consistent_with_and_without_a_parent(self, client, org_id, admin):
        """Regression: `ultimate_parent_of`'s no-parent branch used to return
        a str `entity_id` while a real chain step returned psycopg2's raw
        `uuid.UUID`. The hierarchy API always hands back a plain string
        either way."""
        _login(client)
        child = _org(client, "Child Co")
        parent = _org(client, "Parent Co")
        _own(client, child, parent)

        response = client.get(
            f"/records/organization/{child['id']}/hierarchy", params={"role": "owned_by"}
        )
        step = response.json()["chain"][0]
        assert step["entity_id"] == parent["id"]
        assert isinstance(step["entity_id"], str)


class TestBadRole:
    def test_a_non_hierarchical_role_is_400(self, client, org_id, admin):
        _login(client)
        org = _org(client, "Acme")
        response = client.get(
            f"/records/organization/{org['id']}/hierarchy", params={"role": "vendor_to"}
        )
        assert response.status_code == 400

    def test_an_unknown_role_is_400(self, client, org_id, admin):
        _login(client)
        org = _org(client, "Acme")
        response = client.get(
            f"/records/organization/{org['id']}/hierarchy",
            params={"role": "not_a_real_role"},
        )
        assert response.status_code == 400

    def test_a_bad_direction_is_400(self, client, org_id, admin):
        _login(client)
        org = _org(client, "Acme")
        response = client.get(
            f"/records/organization/{org['id']}/hierarchy",
            params={"role": "owned_by", "direction": "sideways"},
        )
        assert response.status_code == 400


class TestPermissions:
    def test_unauthenticated_is_401(self, client, org_id, admin):
        response = client.get(
            "/records/organization/00000000-0000-0000-0000-000000000000/hierarchy",
            params={"role": "owned_by"},
        )
        assert response.status_code == 401

    def test_no_read_grant_on_the_subject_entity_is_403(self, client, org_id, admin, member):
        _login(client)
        org = _org(client, "Acme")

        _login(client, "member@example.com")
        response = client.get(
            f"/records/organization/{org['id']}/hierarchy", params={"role": "owned_by"}
        )
        assert response.status_code == 403

    def test_an_ancestor_the_caller_cannot_see_is_absent_not_an_error(
        self, client, org_id, principal, member
    ):
        """Same "hidden, not listed" guarantee `related_blocks` gives
        ordinary associations: a step outside the caller's row-level
        visibility simply does not appear in the chain, rather than a 403 or
        a partial error. Both orgs here are owned by admin, so a member
        granted only 'own' visibility can reach the endpoint (entity-level
        read) but the hydration step filters the ancestor out."""
        from server.core import repository

        child = repository.create(principal, "organization", {"name": "Visible Child"})
        parent = repository.create(principal, "organization", {"name": "Hidden Parent"})
        from server.core import associations

        associations.associate(
            principal, role="owned_by", from_type="organization", from_id=str(child["id"]),
            to_type="organization", to_id=str(parent["id"]),
        )

        role = users.create_role(org_id, "OwnOrgs")
        users.set_role_scope(org_id, str(role["id"]), "organization", read_level="own")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))

        _login(client, "member@example.com")
        response = client.get(
            f"/records/organization/{child['id']}/hierarchy", params={"role": "owned_by"}
        )
        assert response.status_code == 200
        assert response.json()["chain"] == []
