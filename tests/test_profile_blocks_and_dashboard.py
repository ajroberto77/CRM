"""Phase 12: role-gated profile blocks and the per-vertical dashboard, at
the HTTP layer -- `GET /records/{entity}/schema`'s `profile_blocks`,
`GET /records/dashboard-tiles`, and `GET /records/dashboard-groups`.
"""
from __future__ import annotations

from server.core import users


def _login(client, email="admin@example.com", password="correct-horse-battery"):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


class TestSchemaProfileBlocks:
    def test_organization_schema_lists_every_applicable_funds_block(self, client, org_id, admin):
        _login(client)
        body = client.get("/records/organization/schema").json()
        keys = {b["key"] for b in body["profile_blocks"]}
        assert keys == {"funds.lp_summary", "funds.gp_summary", "funds.portfolio_summary"}

    def test_person_schema_excludes_the_organization_only_block(self, client, org_id, admin):
        """`funds.portfolio_summary` declares `applies_to=("organization",)` --
        a person's page must never be offered a block it can't use."""
        _login(client)
        body = client.get("/records/person/schema").json()
        keys = {b["key"] for b in body["profile_blocks"]}
        assert keys == {"funds.lp_summary", "funds.gp_summary"}
        assert "funds.portfolio_summary" not in keys

    def test_a_blocks_role_gate_and_region_reach_the_wire(self, client, org_id, admin):
        _login(client)
        body = client.get("/records/organization/schema").json()
        by_key = {b["key"]: b for b in body["profile_blocks"]}
        assert by_key["funds.portfolio_summary"]["region"] == "left"
        assert set(by_key["funds.portfolio_summary"]["roles"]) == {"portfolio_of", "evaluating"}

    def test_an_entity_with_no_blocks_gets_an_empty_list_not_an_error(self, client, org_id, admin):
        _login(client)
        body = client.get("/records/task/schema").json()
        assert body["profile_blocks"] == []

    def test_role_summary_is_omitted_from_the_generic_field_list(self, client, org_id, admin):
        """`role_summary` already renders as role pills (RecordPage.tsx) --
        showing it a second time as a raw jsonb blob in the ordinary field
        list would be pure noise, not a second real presentation."""
        _login(client)
        body = client.get("/records/organization/schema").json()
        assert "role_summary" not in body["fields"]


class TestDashboardEndpoints:
    def test_dashboard_groups_includes_investing(self, client, org_id, admin):
        _login(client)
        groups = client.get("/records/dashboard-groups").json()["nav_groups"]
        assert "Investing" in groups

    def test_dashboard_tiles_lists_every_registered_funds_tile(self, client, org_id, admin):
        _login(client)
        response = client.get("/records/dashboard-tiles", params={"nav_group": "Investing"})
        assert response.status_code == 200
        keys = {t["key"] for t in response.json()["tiles"]}
        assert keys == {
            "funds.funds_by_status", "funds.commitments_by_status", "funds.committed_by_status",
        }

    def test_an_unregistered_nav_group_returns_an_empty_list(self, client, org_id, admin):
        _login(client)
        response = client.get("/records/dashboard-tiles", params={"nav_group": "Not A Real Group"})
        assert response.status_code == 200
        assert response.json()["tiles"] == []

    def test_a_tile_whose_entity_the_principal_cannot_read_is_dropped(
        self, client, org_id, member
    ):
        """Same filtering `list_entities()` already applies to the sidebar --
        a dangling tile that always 403s on its own aggregate call is worse
        than simply not showing it."""
        role = users.create_role(org_id, "Fund Reader Only")
        users.set_role_scope(org_id, str(role["id"]), "fund", read_level="all")
        # Deliberately no grant on "commitment".
        users.assign_role(org_id, str(member["id"]), str(role["id"]))

        _login(client, "member@example.com")
        response = client.get("/records/dashboard-tiles", params={"nav_group": "Investing"})
        keys = {t["key"] for t in response.json()["tiles"]}
        assert keys == {"funds.funds_by_status"}

    def test_dashboard_groups_drops_a_group_with_no_readable_tile(self, client, org_id, member):
        """A vertical whose every tile's entity a role has no grant on must
        not offer its dashboard-link icon at all -- clicking through to an
        always-empty page would read as 'nothing here', not the actual
        'you can't see this' it is."""
        role = users.create_role(org_id, "No Investing Access")
        users.set_role_scope(org_id, str(role["id"]), "organization", read_level="all")
        # Deliberately no grant on "fund" or "commitment".
        users.assign_role(org_id, str(member["id"]), str(role["id"]))

        _login(client, "member@example.com")
        groups = client.get("/records/dashboard-groups").json()["nav_groups"]
        assert "Investing" not in groups

    def test_dashboard_groups_keeps_a_group_with_at_least_one_readable_tile(
        self, client, org_id, member
    ):
        role = users.create_role(org_id, "Fund Reader Only")
        users.set_role_scope(org_id, str(role["id"]), "fund", read_level="all")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))

        _login(client, "member@example.com")
        groups = client.get("/records/dashboard-groups").json()["nav_groups"]
        assert "Investing" in groups
