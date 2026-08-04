"""`registry.verify()` -- the startup safety net for bad declarations.

Each check here corresponds to a way a bad `EntitySpec`/`FieldSpec` would
otherwise surface as a runtime SQL error (a `compute` field silently reading
the wrong column, per the module docstring) or a dangling link a browser only
discovers when a user clicks it -- rather than failing at boot the way a bad
migration already does. This file is the proof each class of bad declaration
is actually caught, not just documented in `verify()`'s comments.
"""
from __future__ import annotations

import pytest

from server.core import registry
from server.db import schema

# `owner_id` is every entity's spine field (registry.spine()) and always
# points at a user, never a registry entity -- exempted by name rather than
# enumerated per entity. `account_id`/`verified_by`/`reviewed_by`/
# `submitted_by` are the same story for the handful of fields that point at
# connected accounts or users specifically (accounts are exposed through
# server/api/accounts.py, not the generic CRUD path; there is no registered
# "user" entity). Everything else ending in `_id` is expected to declare a
# target.
_NO_TARGET_FIELD_NAMES = {"owner_id"}
_NO_TARGET_EXEMPT = {
    ("interaction", "account_id"),
    ("investor_profile", "verified_by"),
    ("investor_mandate", "reviewed_by"),
    ("questionnaire_response", "submitted_by"),
}


@pytest.fixture
def temp_entity():
    """Registers an EntitySpec for one test, then removes it.

    `registry.register()` has no public unregister -- reaching into
    `_ENTITIES` is acceptable here because this file specifically tests the
    registry's own internals, not calling it as a black box the way every
    other test file does.
    """
    registered: list[str] = []

    def _make(spec: registry.EntitySpec) -> registry.EntitySpec:
        registry.register(spec)
        registered.append(spec.name)
        return spec

    yield _make
    for name in registered:
        registry._ENTITIES.pop(name, None)


def _base_fields(**extra: registry.FieldSpec) -> dict[str, registry.FieldSpec]:
    fields = dict(registry.spine({
        "name": registry.FieldSpec("name", "text", column="name"),
    }))
    fields.update(extra)
    return fields


class TestVerifyOnRealDeclarations:
    def test_the_installed_registry_is_internally_consistent(self):
        """Phase 0 hand-added references/nav/list_columns/label_field across
        ~30 fields in three files. Run verify() against the real, fully
        installed registry and the real schema, exactly as the app lifespan
        does, so a typo in any of them fails here rather than at boot."""
        assert registry.verify(schema.all_tables()) == []

    def test_every_registered_reference_field_declares_a_target(self):
        """Every `_id` field ending a real foreign key names either a fixed
        target (`references`) or the sibling field naming it per-row
        (`references_type_field`) -- except the handful that intentionally
        point outside the registry (users, connected accounts)."""
        missing = []
        for spec in registry.all_entities():
            for field_name, fspec in spec.fields.items():
                if fspec.kind != "uuid" or field_name == "id":
                    continue
                if field_name in _NO_TARGET_FIELD_NAMES:
                    continue
                if (spec.name, field_name) in _NO_TARGET_EXEMPT:
                    continue
                if fspec.references is None and fspec.references_type_field is None:
                    missing.append(f"{spec.name}.{field_name}")
        assert missing == []


class TestComputedFieldFilterable:
    def test_a_compute_field_left_filterable_is_rejected(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_computed_filterable", table="core.organizations",
            fields=_base_fields(derived=registry.FieldSpec(
                "derived", "text", compute=lambda row, ctx: "x",
            )),
            module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert {"entity": spec.name, "problem": "computed_field_filterable",
                "field": "derived"} in problems

    def test_a_compute_field_marked_unfilterable_is_accepted(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_computed_ok", table="core.organizations",
            fields=_base_fields(derived=registry.FieldSpec(
                "derived", "text", filterable=False, sortable=False,
                compute=lambda row, ctx: "x",
            )),
            module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert not any(p["entity"] == spec.name for p in problems)


class TestReferences:
    def test_a_dangling_reference_is_rejected(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_dangling_ref", table="core.organizations",
            fields=_base_fields(widget_id=registry.FieldSpec(
                "widget_id", "uuid", references="widget_that_does_not_exist",
            )),
            module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert {"entity": spec.name, "problem": "reference_unknown_entity",
                "field": "widget_id", "references": "widget_that_does_not_exist"} in problems

    def test_a_reference_to_a_real_entity_is_accepted(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_real_ref", table="core.organizations",
            fields=_base_fields(other_id=registry.FieldSpec(
                "other_id", "uuid", references="person",
            )),
            module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert not any(p["entity"] == spec.name for p in problems)

    def test_a_dangling_reference_type_field_is_rejected(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_dangling_type_field", table="core.organizations",
            fields=_base_fields(subject_id=registry.FieldSpec(
                "subject_id", "uuid", references_type_field="subject_type",
            )),
            module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert {"entity": spec.name, "problem": "reference_type_field_missing",
                "field": "subject_id", "references_type_field": "subject_type"} in problems

    def test_a_field_cannot_declare_both_reference_kinds(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_both_ref_kinds", table="core.organizations",
            fields=_base_fields(
                subject_type=registry.FieldSpec("subject_type", "text"),
                subject_id=registry.FieldSpec(
                    "subject_id", "uuid", references="person",
                    references_type_field="subject_type",
                ),
            ),
            module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert {"entity": spec.name, "problem": "field_has_both_reference_kinds",
                "field": "subject_id"} in problems


class TestReverseReferences:
    """`reverse_references()` -- what `repository.children_of()` walks to find
    a record's "children," the FK-based counterpart to `related_blocks()`'s
    association edges."""

    def test_a_fixed_reference_is_found(self):
        refs = registry.reverse_references("fund")
        assert {"entity": "commitment", "field": "fund_id", "polymorphic": False} in refs

    def test_a_polymorphic_reference_is_found_for_any_target(self):
        # task.subject_id's target varies per row (checked at query time by
        # children_of(), not here) -- every entity is a candidate target,
        # including one with no real task rows pointing at it yet.
        refs = registry.reverse_references("fund")
        assert {
            "entity": "task", "field": "subject_id",
            "polymorphic": True, "type_field": "subject_type",
        } in refs

    def test_an_entity_nothing_points_at_has_no_fixed_references(self):
        # custom_field is never a FieldSpec.references target anywhere in the
        # registry -- only the always-present polymorphic candidates appear.
        refs = registry.reverse_references("custom_field")
        assert not any(not r["polymorphic"] for r in refs)


class TestLabelField:
    def test_a_label_field_naming_no_real_field_is_rejected(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_bad_label_field", table="core.organizations",
            label_field="nope", fields=_base_fields(), module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert {"entity": spec.name, "problem": "label_field_missing",
                "field": "nope"} in problems

    def test_a_computed_label_field_without_searchable_is_rejected(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_computed_label_no_search", table="core.organizations",
            label_field="display_label", searchable=(),
            fields=_base_fields(display_label=registry.FieldSpec(
                "display_label", "text", filterable=False, sortable=False,
                compute=lambda row, ctx: "x",
            )),
            module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert {"entity": spec.name,
                "problem": "computed_label_field_needs_searchable"} in problems

    def test_a_computed_label_field_with_searchable_is_accepted(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_computed_label_with_search", table="core.organizations",
            label_field="display_label", searchable=("name",),
            fields=_base_fields(display_label=registry.FieldSpec(
                "display_label", "text", filterable=False, sortable=False,
                compute=lambda row, ctx: "x",
            )),
            module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert not any(p["entity"] == spec.name for p in problems)


class TestListColumns:
    def test_a_list_column_naming_no_real_field_is_rejected(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_bad_list_column", table="core.organizations",
            list_columns=("name", "typo_field"), fields=_base_fields(),
            module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert {"entity": spec.name, "problem": "list_column_missing",
                "field": "typo_field"} in problems


class TestNav:
    def test_an_unrecognized_nav_value_is_rejected(self, temp_entity):
        spec = temp_entity(registry.EntitySpec(
            name="_test_bad_nav", table="core.organizations",
            nav="sidebar", fields=_base_fields(), module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert {"entity": spec.name, "problem": "bad_nav", "nav": "sidebar"} in problems

    @pytest.mark.parametrize("nav", ["", "primary", "settings", "none"])
    def test_every_closed_vocabulary_value_is_accepted(self, temp_entity, nav):
        spec = temp_entity(registry.EntitySpec(
            name="_test_nav_ok", table="core.organizations",
            nav=nav, fields=_base_fields(), module="test",
        ))
        problems = registry.verify(schema.all_tables())
        assert not any(p["entity"] == spec.name for p in problems)
