"""The EspoCRM-shaped permission model: permissive merge, visibility levels,
field masking, and the admin split."""
from __future__ import annotations

import pytest

from server.core import permissions, users
from server.core.permissions import ObjectPermissions, Principal


class TestMerge:
    def test_widest_wins(self):
        assert permissions.widest("own", "all") == "all"
        assert permissions.widest("none", "own") == "own"
        assert permissions.widest("team", "own") == "team"
        assert permissions.widest() == "none"

    def test_two_roles_merge_permissively(self, org_id, member):
        """If any role grants it, the user has it."""
        narrow = users.create_role(org_id, "Narrow")
        wide = users.create_role(org_id, "Wide")
        users.set_role_scope(org_id, str(narrow["id"]), "person", read_level="own")
        users.set_role_scope(
            org_id, str(wide["id"]), "person", can_create=True, read_level="all"
        )
        users.assign_role(org_id, str(member["id"]), str(narrow["id"]))
        users.assign_role(org_id, str(member["id"]), str(wide["id"]))

        perms = permissions.load_principal(member).for_object("person")
        assert perms.read_level == "all"
        assert perms.can_create is True

    def test_object_with_no_scope_row_is_denied(self, org_id, member):
        principal = permissions.load_principal(member)
        assert principal.for_object("deal").read_level == "none"
        assert not principal.for_object("deal").allows("read")


class TestAdminSplit:
    def test_admin_has_everything_without_any_role(self, admin):
        principal = permissions.load_principal(admin)
        for action in permissions.ACTIONS:
            assert principal.for_object("anything_at_all").allows(action)

    def test_member_is_not_admin(self, member):
        principal = permissions.load_principal(member)
        with pytest.raises(permissions.PermissionDenied):
            principal.require_admin()


class TestRequire:
    def test_require_raises_rather_than_returning_false(self, org_id, member):
        """A gate that returns a falsy value lets a forgotten `if` become an
        open door, and a gate that no-ops lets the caller mark the work done.
        Both are real defects from the sibling projects."""
        principal = permissions.load_principal(member)
        with pytest.raises(permissions.PermissionDenied):
            principal.require("edit", "person")

    def test_require_rejects_unknown_action(self, member):
        principal = permissions.load_principal(member)
        with pytest.raises(ValueError):
            principal.require("frobnicate", "person")


class TestVisibilityPredicate:
    def _principal(self, level: str, *, team_id=None) -> Principal:
        return Principal(
            user_id="11111111-1111-1111-1111-111111111111",
            org_id="22222222-2222-2222-2222-222222222222",
            team_id=team_id,
            permissions={"person": ObjectPermissions(object="person", read_level=level)},
        )

    def test_all(self):
        sql, params = permissions.visibility_predicate(self._principal("all"), "person")
        assert sql == "TRUE" and params == []

    def test_none_is_false_not_an_exception(self):
        """A list endpoint should return an empty page, not a 403. Callers
        needing a hard failure call require() first."""
        sql, params = permissions.visibility_predicate(self._principal("none"), "person")
        assert sql == "FALSE" and params == []

    def test_own(self):
        sql, params = permissions.visibility_predicate(self._principal("own"), "person")
        assert "owner_id = %s" in sql
        assert params == ["11111111-1111-1111-1111-111111111111"]

    def test_team(self):
        principal = self._principal("team", team_id="33333333-3333-3333-3333-333333333333")
        sql, params = permissions.visibility_predicate(principal, "person")
        assert "team_id = %s" in sql
        assert params[-1] == "33333333-3333-3333-3333-333333333333"

    def test_team_without_a_team_degrades_to_own(self):
        """Treating a null team as matching every null-team row would expose
        every unassigned record in the org."""
        sql, params = permissions.visibility_predicate(self._principal("team"), "person")
        assert sql == "t.owner_id = %s"
        assert params == ["11111111-1111-1111-1111-111111111111"]

    def test_predicate_does_not_repeat_the_org_boundary(self):
        """RLS already enforces it; restating it here would be a second
        implementation of the same rule (R1)."""
        sql, _ = permissions.visibility_predicate(self._principal("all"), "person")
        assert "org_id" not in sql


class TestFieldMasks:
    def test_mask_applies_only_when_every_role_masks_it(self, org_id, member):
        """A mask is a ceiling and the merge is permissive: holding one role
        that leaves a field unrestricted leaves it unrestricted."""
        masked = users.create_role(org_id, "Masked")
        open_role = users.create_role(org_id, "Open")
        for role in (masked, open_role):
            users.set_role_scope(org_id, str(role["id"]), "person", read_level="all")
        users.set_field_mask(org_id, str(masked["id"]), "person", "salary", "none")

        users.assign_role(org_id, str(member["id"]), str(masked["id"]))
        principal = permissions.load_principal(member)
        assert principal.for_object("person").field_masks == {"salary": "none"}

        users.assign_role(org_id, str(member["id"]), str(open_role["id"]))
        principal = permissions.load_principal(member)
        assert principal.for_object("person").field_masks == {}

    def test_mask_record_strips_hidden_fields(self):
        principal = Principal(
            user_id="u",
            org_id="o",
            permissions={
                "person": ObjectPermissions(object="person", field_masks={"salary": "none"})
            },
        )
        record = {"id": "1", "name": "Bob", "salary": 100}
        assert permissions.mask_record(principal, "person", record) == {"id": "1", "name": "Bob"}

    def test_write_to_a_masked_field_raises_rather_than_dropping_it(self):
        """Silently dropping the field would report success for a write that
        did not happen."""
        principal = Principal(
            user_id="u",
            org_id="o",
            permissions={
                "person": ObjectPermissions(object="person", field_masks={"salary": "read"})
            },
        )
        with pytest.raises(permissions.PermissionDenied):
            permissions.reject_masked_writes(principal, "person", {"salary": 200})

        permissions.reject_masked_writes(principal, "person", {"name": "Bob"})
