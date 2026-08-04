"""`POST /records/labels` (server/api/records.py, server/core/repository.py's
`resolve_labels()`) -- decision B of the relationship-product rework plan.

The reference tables most in need of a human label (`pipeline`,
`investor_category`, `gp_role`, ...) are `admin_only`, and `list_records`/
`get_record` correctly hard-403 a non-admin on them entity-wide. This
endpoint is the deliberate carve-out: a member viewing a record with a
`pipeline_id` must see the pipeline's name, not a uuid, without personally
administering pipelines -- the same shape `custom_field_defs()` already
uses for schema metadata.
"""
from __future__ import annotations

from server.core import repository, users


def _login(client, email="admin@example.com", password="correct-horse-battery"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


class TestResolvesAdminOnlyReferenceData:
    def test_a_member_resolves_an_admin_only_labels_without_being_admin(
        self, client, org_id, principal, member
    ):
        pipeline = repository.create(principal, "pipeline", {"name": "Standard Pipeline"})

        role = users.create_role(org_id, "Reader")
        users.set_role_scope(org_id, str(role["id"]), "pipeline", read_level="all")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))

        # The member could never LIST pipelines (admin_only 403s list_records
        # entity-wide) -- this endpoint is the deliberate exception.
        _login(client, "member@example.com")
        listed = client.get("/records/pipeline")
        assert listed.status_code == 403

        resolved = client.post(
            "/records/labels", json={"refs": {"pipeline": [str(pipeline["id"])]}}
        )
        assert resolved.status_code == 200
        assert resolved.json()["labels"] == {
            "pipeline": {str(pipeline["id"]): "Standard Pipeline"}
        }

    def test_a_member_with_no_grant_at_all_is_403(
        self, client, org_id, principal, member
    ):
        """`resolve_labels` requires ordinary `read`, same as every other
        entity -- a member with zero grant on `pipeline` is refused, exactly
        like every other unpermissioned read."""
        pipeline = repository.create(principal, "pipeline", {"name": "Standard Pipeline"})
        _login(client, "member@example.com")
        response = client.post(
            "/records/labels", json={"refs": {"pipeline": [str(pipeline["id"])]}}
        )
        assert response.status_code == 403


class TestInvisibleIdsAreOmitted:
    def test_a_row_outside_visibility_is_silently_absent(
        self, client, org_id, principal, member
    ):
        """This is a label lookup, not an existence oracle: an id the caller
        cannot see is simply missing from the result, never a 404 or a
        placeholder."""
        from server.core import permissions

        admins_note = repository.create(principal, "note", {"body": "Admin's private note"})
        role = users.create_role(org_id, "OwnNotes")
        users.set_role_scope(
            org_id, str(role["id"]), "note", can_create=True, read_level="own"
        )
        users.assign_role(org_id, str(member["id"]), str(role["id"]))
        # Loaded AFTER the role is assigned -- load_principal() reads scopes
        # at call time, unlike the session-shaped `member_principal` fixture,
        # which would have been resolved before this role existed.
        member_after_grant = permissions.load_principal(member)
        members_note = repository.create(member_after_grant, "note", {"body": "Member's own note"})

        _login(client, "member@example.com")
        response = client.post(
            "/records/labels",
            json={"refs": {"note": [str(admins_note["id"]), str(members_note["id"])]}},
        )
        assert response.status_code == 200
        assert response.json()["labels"] == {
            "note": {str(members_note["id"]): "Member's own note"}
        }


class TestComputedLabels:
    def test_a_computed_label_field_resolves_through_the_same_endpoint(
        self, client, org_id, admin
    ):
        """Phase 0 gave `commitment` a computed `display_label` (amount +
        currency) instead of the raw `amount` -- resolve_labels must run the
        same `_finalize_read`/compute path list/get already do, not select
        the label_field as a bare column (it isn't one)."""
        _login(client)
        fund = client.post(
            "/records/fund", json={"name": "Northgate Fund II"}
        ).json()["record"]
        org = client.post(
            "/records/organization", json={"name": "Brightline Capital"}
        ).json()["record"]
        commitment = client.post(
            "/records/commitment",
            json={
                "fund_id": fund["id"], "investor_org_id": org["id"],
                "amount": 25_000_000, "currency": "USD",
                "committed_at": "2026-03-01", "status": "signed",
            },
        ).json()["record"]

        resolved = client.post(
            "/records/labels", json={"refs": {"commitment": [commitment["id"]]}}
        )
        assert resolved.status_code == 200
        label = resolved.json()["labels"]["commitment"][commitment["id"]]
        assert "USD" in label
        assert label != commitment["id"]


class TestUnknownReferences:
    def test_an_unknown_entity_in_refs_is_404(self, client, org_id, admin):
        _login(client)
        response = client.post(
            "/records/labels", json={"refs": {"not_a_real_entity": ["x"]}}
        )
        assert response.status_code == 404

    def test_an_empty_refs_body_resolves_to_an_empty_result(self, client, org_id, admin):
        _login(client)
        response = client.post("/records/labels", json={"refs": {}})
        assert response.status_code == 200
        assert response.json()["labels"] == {}

    def test_unauthenticated_is_401(self, client, org_id, admin):
        response = client.post("/records/labels", json={"refs": {}})
        assert response.status_code == 401
