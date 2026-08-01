"""The one CRUD path: registry, filter compiler, repository, events.

The bar for this milestone is that a new entity is a registry entry and nothing
else, so several of these tests assert properties of the mechanism rather than
of any particular entity.
"""
from __future__ import annotations

import pytest

from server.core import events, permissions, query, registry, repository, users
from server.db import schema


class TestRegistry:
    def test_every_registered_entity_matches_the_real_schema(self):
        """Catches the failure this check exists for: visibility_predicate
        hardcodes owner_id, so an entity whose table lacks it produces invalid
        SQL only when a NON-ADMIN queries it -- which every test using the
        admin fixture would sail straight past."""
        assert registry.verify(schema.all_tables()) == []

    def test_declared_tables_all_exist(self):
        for spec in registry.all_entities():
            assert spec.table in schema.all_tables()

    def test_unknown_entity_and_field_raise(self):
        with pytest.raises(registry.UnknownEntity):
            registry.entity("nonexistent")
        with pytest.raises(registry.UnknownField):
            registry.field_spec("person", "nonexistent")

    def test_custom_key_pattern_rejects_injection_shaped_keys(self):
        for bad in ["x' OR '1'='1", "DROP TABLE", "Upper", "1leading", "has-dash", ""]:
            assert not registry.CUSTOM_KEY_RE.match(bad), bad
        for good in ["renewal", "arr_2026", "x"]:
            assert registry.CUSTOM_KEY_RE.match(good)

    def test_roles_reference_registered_entities(self):
        known = set(registry.entities())
        for role in registry.roles():
            assert set(role.from_types) <= known
            assert set(role.to_types) <= known


class TestFilterCompiler:
    def test_unknown_field_raises_rather_than_being_dropped(self, principal):
        """A dropped filter returns MORE rows than were asked for -- a
        disclosure bug wearing a UX bug's clothes."""
        with pytest.raises(registry.UnknownField):
            query.compile_filter(
                "person", {"field": "salary", "op": "eq", "value": 1}, principal=principal
            )

    @pytest.mark.parametrize(
        "field",
        ["x'; DROP TABLE core.persons--", "custom.x' OR '1'='1", "t.owner_id"],
    )
    def test_injection_shaped_keys_are_rejected_not_escaped(self, principal, field):
        with pytest.raises((registry.UnknownField, query.FilterError)):
            query.compile_filter(
                "person", {"field": field, "op": "eq", "value": 1}, principal=principal
            )

    def test_values_are_always_parameterized(self, principal):
        compiled = query.compile_filter(
            "person",
            {"field": "full_name", "op": "eq", "value": "'; DROP TABLE core.persons--"},
            principal=principal,
        )
        assert "DROP TABLE" not in compiled.sql
        assert compiled.params == ["'; DROP TABLE core.persons--"]

    def test_custom_key_is_bound_never_interpolated(self, principal):
        custom = [{"entity": "person", "key": "renewal", "kind": "date",
                   "options": [], "archived_at": None}]
        compiled = query.compile_filter(
            "person", {"field": "custom.renewal", "op": "gt", "value": "2026-01-01"},
            principal=principal, custom_fields=custom,
        )
        assert "renewal" not in compiled.sql       # the key is a parameter
        assert "core.jsonb_date" in compiled.sql
        assert compiled.params[0] == "renewal"

    def test_operator_must_suit_the_field_kind(self, principal):
        with pytest.raises(query.FilterError):
            query.compile_filter(
                "person", {"field": "full_name", "op": "between", "value": [1, 2]},
                principal=principal,
            )

    def test_in_rejects_a_bare_string(self, principal):
        """str is iterable, so a permissive implementation explodes it into one
        clause per character -- the same shape as the bug that once sent one
        calendar invite per character of an email address."""
        with pytest.raises(query.FilterError):
            query.compile_filter(
                "person", {"field": "title", "op": "in", "value": "COO"},
                principal=principal,
            )

    def test_impossible_date_is_rejected(self, principal):
        with pytest.raises(query.FilterError):
            query.compile_filter(
                "deal", {"field": "expected_close_on", "op": "eq", "value": "2026-13-45"},
                principal=principal,
            )

    def test_like_wildcards_in_a_value_are_escaped(self, principal):
        compiled = query.compile_filter(
            "person", {"field": "full_name", "op": "contains", "value": "100%_x"},
            principal=principal,
        )
        assert compiled.params == [r"%100\%\_x%"]

    def test_nesting_and_clause_budgets(self, principal):
        deep = {"field": "full_name", "op": "eq", "value": "x"}
        for _ in range(query.MAX_DEPTH + 2):
            deep = {"and": [deep]}
        with pytest.raises(query.FilterError):
            query.compile_filter("person", deep, principal=principal)

    def test_sort_always_ends_with_a_total_order(self, principal):
        compiled = query.compile_sort("person", None, principal=principal)
        assert compiled.sql.rstrip().endswith("t.id DESC")

    def test_sort_direction_comes_from_an_allowlist(self, principal):
        with pytest.raises(query.FilterError):
            query.compile_sort(
                "person", [{"field": "full_name", "direction": "asc; DROP TABLE x"}],
                principal=principal,
            )


class TestCrud:
    def test_create_read_update_delete(self, principal):
        org = repository.create(principal, "organization", {"name": "Acme"})
        assert org["name"] == "Acme"

        fetched = repository.get_record(principal, "organization", str(org["id"]))
        assert fetched["id"] == org["id"]

        updated = repository.update(
            principal, "organization", str(org["id"]), {"domain": "acme.com"}
        )
        assert updated["domain"] == "acme.com"

        assert repository.delete(principal, "organization", str(org["id"])) is True
        with pytest.raises(repository.NotFound):
            repository.get_record(principal, "organization", str(org["id"]))

    def test_normalization_goes_through_the_one_normalizer(self, principal):
        person = repository.create(
            principal, "person",
            {"full_name": "  Robin   Ferrante ", "primary_email": "R.Ferrante@ACME.com"},
        )
        assert person["primary_email"] == "r.ferrante@acme.com"
        assert person["name_normalized"] == "robin ferrante"

    def test_invalid_email_is_refused(self, principal):
        with pytest.raises(repository.ValidationError):
            repository.create(
                principal, "person", {"full_name": "X", "primary_email": "nope"}
            )

    @pytest.mark.parametrize(
        "values",
        [
            {"name": "D", "expected_close_on": "2026-13-45"},   # to_date would accept
            {"name": "D", "amount": "not-a-number"},
            {"name": "D", "status": "invented"},                 # closed vocabulary
        ],
    )
    def test_every_field_is_type_validated(self, principal, values):
        with pytest.raises(repository.ValidationError):
            repository.create(principal, "deal", values)

    def test_required_fields_are_enforced(self, principal):
        with pytest.raises(repository.ValidationError):
            repository.create(principal, "organization", {"domain": "acme.com"})

    def test_system_fields_are_not_writable(self, principal):
        with pytest.raises(registry.UnknownField):
            repository.create(principal, "organization", {"name": "A", "org_id": "x"})

    def test_missing_record_is_not_found(self, principal):
        with pytest.raises(repository.NotFound):
            repository.get_record(
                principal, "organization", "00000000-0000-0000-0000-000000000001"
            )


class TestCustomFields:
    def _declare(self, principal, key="renewal", kind="date"):
        return repository.create(
            principal, "custom_field",
            {"entity": "person", "key": key, "kind": kind, "label": key},
        )

    def test_custom_values_round_trip(self, principal):
        self._declare(principal)
        person = repository.create(
            principal, "person",
            {"full_name": "Robin", "custom": {"renewal": "2026-09-30"}},
        )
        assert person["custom"]["renewal"] == "2026-09-30"

    def test_undeclared_custom_key_is_refused(self, principal):
        with pytest.raises(registry.UnknownField):
            repository.create(
                principal, "person", {"full_name": "X", "custom": {"undeclared": 1}}
            )

    def test_custom_value_is_type_validated(self, principal):
        self._declare(principal)
        with pytest.raises(repository.ValidationError):
            repository.create(
                principal, "person",
                {"full_name": "X", "custom": {"renewal": "2026-13-45"}},
            )

    def test_a_custom_field_can_actually_be_blanked(self, principal):
        """`custom || %s` is a shallow merge, so without an explicit clear list
        a value could be set but never removed -- safety rule 2 reappearing
        where nobody expects it."""
        self._declare(principal)
        person = repository.create(
            principal, "person",
            {"full_name": "Robin", "custom": {"renewal": "2026-09-30"}},
        )
        cleared = repository.update(
            principal, "person", str(person["id"]), {}, clear=["renewal"]
        )
        assert "renewal" not in cleared["custom"]

    def test_filter_and_sort_on_a_custom_field(self, principal):
        self._declare(principal, key="arr", kind="number")
        for name, arr in [("A", 100), ("B", 900)]:
            repository.create(
                principal, "person", {"full_name": name, "custom": {"arr": arr}}
            )
        result = repository.list_records(
            principal, "person",
            filters={"field": "custom.arr", "op": "gt", "value": 500},
            sort=[{"field": "custom.arr", "direction": "desc"}],
        )
        assert result["total"] == 1
        assert result["records"][0]["full_name"] == "B"


class TestPermissions:
    def _member_with(self, org_id, member, **levels):
        role = users.create_role(org_id, "Members")
        users.set_role_scope(org_id, str(role["id"]), "person", **levels)
        users.assign_role(org_id, str(member["id"]), str(role["id"]))
        return permissions.load_principal(member)

    def test_own_level_lists_only_your_rows(self, org_id, principal, member):
        actor = self._member_with(
            org_id, member, can_create=True, read_level="own", edit_level="own"
        )
        repository.create(principal, "person", {"full_name": "Admin's"})
        repository.create(actor, "person", {"full_name": "Member's"})

        visible = repository.list_records(actor, "person")
        assert [r["full_name"] for r in visible["records"]] == ["Member's"]
        assert visible["total"] == 1, "count must respect visibility too"

    def test_invisible_row_is_not_found_not_forbidden(self, org_id, principal, member):
        """403 on an invisible row confirms it exists -- exactly what the
        visibility level was meant to hide."""
        actor = self._member_with(org_id, member, can_create=True, read_level="own")
        hidden = repository.create(principal, "person", {"full_name": "Admin's"})
        with pytest.raises(repository.NotFound):
            repository.get_record(actor, "person", str(hidden["id"]))

    def test_readable_but_not_editable_raises_permission_denied(
        self, org_id, principal, member
    ):
        actor = self._member_with(org_id, member, read_level="all", edit_level="own")
        theirs = repository.create(principal, "person", {"full_name": "Admin's"})
        repository.get_record(actor, "person", str(theirs["id"]))  # readable
        with pytest.raises(permissions.PermissionDenied):
            repository.update(actor, "person", str(theirs["id"]), {"title": "X"})

    def test_no_permission_at_all_raises(self, member_principal):
        with pytest.raises(permissions.PermissionDenied):
            repository.list_records(member_principal, "person")

    def test_masked_field_is_absent_and_unwritable(self, org_id, principal, member):
        role = users.create_role(org_id, "Masked")
        users.set_role_scope(
            org_id, str(role["id"]), "person",
            can_create=True, read_level="all", edit_level="all",
        )
        users.set_field_mask(org_id, str(role["id"]), "person", "title", "none")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))
        actor = permissions.load_principal(member)

        created = repository.create(principal, "person",
                                    {"full_name": "Robin", "title": "COO"})

        assert "title" not in repository.get_record(actor, "person", str(created["id"]))
        assert all("title" not in r for r in
                   repository.list_records(actor, "person")["records"])
        with pytest.raises(permissions.PermissionDenied):
            repository.update(actor, "person", str(created["id"]), {"title": "CEO"})

    def test_filtering_on_a_masked_field_is_refused(self, org_id, principal, member):
        """Otherwise it is a disclosure oracle: the filter returns exactly the
        right rows even though the value never appears in a response, so it is
        recoverable by binary search."""
        role = users.create_role(org_id, "Masked")
        users.set_role_scope(org_id, str(role["id"]), "person", read_level="all")
        users.set_field_mask(org_id, str(role["id"]), "person", "title", "none")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))
        actor = permissions.load_principal(member)

        with pytest.raises(permissions.PermissionDenied):
            repository.list_records(
                actor, "person", filters={"field": "title", "op": "eq", "value": "COO"}
            )
        with pytest.raises(permissions.PermissionDenied):
            repository.list_records(actor, "person", sort=[{"field": "title"}])

    def test_owner_id_needs_more_than_own_level(self, org_id, principal, member):
        """A user at 'own' level could otherwise reassign a record away from
        themselves, or steal one by assigning it to themselves."""
        actor = self._member_with(
            org_id, member, can_create=True, read_level="own", edit_level="own"
        )
        mine = repository.create(actor, "person", {"full_name": "Mine"})
        with pytest.raises(permissions.PermissionDenied):
            repository.update(
                actor, "person", str(mine["id"]), {"owner_id": str(principal.user_id)}
            )

    def test_create_forces_owner_to_the_acting_user(self, member_principal, org_id, member):
        actor = self._member_with(org_id, member, can_create=True, read_level="own")
        created = repository.create(actor, "person", {"full_name": "Mine"})
        assert str(created["owner_id"]) == actor.user_id

    def test_admin_only_entities_refuse_members(self, org_id, member):
        role = users.create_role(org_id, "Members")
        users.set_role_scope(org_id, str(role["id"]), "pipeline", read_level="all")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))
        actor = permissions.load_principal(member)
        with pytest.raises(permissions.PermissionDenied):
            repository.list_records(actor, "pipeline")


class TestConcurrency:
    def test_stale_write_is_rejected(self, principal):
        person = repository.create(principal, "person", {"full_name": "Robin"})
        stale = person["updated_at"]
        repository.update(principal, "person", str(person["id"]), {"title": "COO"})
        with pytest.raises(repository.Conflict):
            repository.update(
                principal, "person", str(person["id"]),
                {"title": "CEO"}, if_unmodified_since=stale,
            )


class TestEvents:
    def test_create_and_update_emit_with_a_field_level_diff(self, principal):
        seen = []
        events.subscribe("probe", seen.append)

        person = repository.create(principal, "person", {"full_name": "Robin"})
        repository.update(principal, "person", str(person["id"]), {"title": "COO"})

        assert [e.action for e in seen] == [events.CREATED, events.UPDATED]
        assert seen[1].diff["title"] == {"before": "", "after": "COO"}
        assert not seen[1].changed("full_name")

    def test_a_no_op_update_emits_nothing(self, principal):
        """Inline in-cell editing fires a save on every blur; without this, an
        untouched field becomes a message per blur in M5."""
        person = repository.create(principal, "person", {"full_name": "Robin"})
        seen = []
        events.subscribe("probe", seen.append)
        repository.update(principal, "person", str(person["id"]), {"full_name": "Robin"})
        assert seen == []

    def test_a_throwing_subscriber_does_not_lose_the_write(self, principal):
        """The write already committed. Re-raising would return 500 for a
        successful write, and the client's retry would duplicate the record."""
        def boom(event):
            raise RuntimeError("subscriber exploded")

        events.subscribe("boom", boom)
        person = repository.create(principal, "person", {"full_name": "Robin"})
        assert repository.get_record(principal, "person", str(person["id"]))

    def test_events_are_recorded_in_the_outbox(self, principal, org_id):
        from server.db import pool as dbpool

        repository.create(principal, "person", {"full_name": "Robin"})
        with dbpool.transaction(org_id=org_id, readonly=True) as cur:
            cur.execute(
                "SELECT entity, action FROM core.events WHERE entity = 'person'"
            )
            rows = [dict(r) for r in cur.fetchall()]
        assert rows and rows[0]["action"] == events.CREATED

    def test_subscriber_diff_descends_into_custom(self, principal):
        repository.create(
            principal, "custom_field",
            {"entity": "person", "key": "arr", "kind": "number", "label": "ARR"},
        )
        person = repository.create(principal, "person", {"full_name": "Robin"})
        seen = []
        events.subscribe("probe", seen.append)
        repository.update(principal, "person", str(person["id"]),
                          {"custom": {"arr": 100}})
        assert "custom.arr" in seen[0].diff


class TestSystemPrincipal:
    def test_it_requires_a_reason_and_is_marked(self, org_id):
        with pytest.raises(ValueError):
            permissions.system_principal(org_id, "")
        actor = permissions.system_principal(org_id, "contact_sync")
        assert actor.is_system and actor.system_reason == "contact_sync"

    def test_system_writes_are_attributed_in_the_event_log(self, org_id):
        actor = permissions.system_principal(org_id, "contact_sync")
        seen = []
        events.subscribe("probe", seen.append)
        repository.create(actor, "organization", {"name": "Derived Co"})
        assert seen[0].actor_kind == "system:contact_sync"
