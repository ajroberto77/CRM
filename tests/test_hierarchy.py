"""Ancestor/descendant traversal for hierarchical association roles, and the
write-time cycle guard that makes an uncapped recursive walk safe.

Exercises `server/core/hierarchy.py` through `owned_by` -- the core,
domain-neutral hierarchical role (legal/corporate ownership, from=child/
owned org, to=parent/owner) -- rather than a throwaway test-only role, so
this doubles as the role's own test coverage.
"""
from __future__ import annotations

import pytest

from server.core import associations, hierarchy, registry, repository


@pytest.fixture
def parent(principal):
    return repository.create(principal, "organization", {"name": "Meridian Holdings"})


@pytest.fixture
def child(principal):
    return repository.create(principal, "organization", {"name": "Meridian Advisory"})


@pytest.fixture
def grandchild(principal):
    return repository.create(principal, "organization", {"name": "Meridian Advisory EU"})


def _link(principal, child_id, parent_id, **kw):
    return associations.associate(
        principal, role="owned_by", from_type="organization", from_id=str(child_id),
        to_type="organization", to_id=str(parent_id), **kw,
    )


class TestTraversal:
    def test_a_four_level_chain_rolls_up_correctly(self, principal):
        """gg -> g -> p -> top. `ultimate_parent_of` on the great-grandchild
        must resolve all the way to `top`, and `ancestors_of` must list every
        intermediate rung, nearest first."""
        top = repository.create(principal, "organization", {"name": "Top Co"})
        p = repository.create(principal, "organization", {"name": "Parent Co"})
        g = repository.create(principal, "organization", {"name": "Grandchild Co"})
        gg = repository.create(principal, "organization", {"name": "Great-grandchild Co"})

        _link(principal, p["id"], top["id"])
        _link(principal, g["id"], p["id"])
        _link(principal, gg["id"], g["id"])

        chain = hierarchy.ancestors_of(principal, "owned_by", "organization", str(gg["id"]))
        ids_in_order = [str(c["entity_id"]) for c in chain]
        assert ids_in_order == [str(g["id"]), str(p["id"]), str(top["id"])]

        ultimate = hierarchy.ultimate_parent_of(principal, "owned_by", "organization", str(gg["id"]))
        assert str(ultimate["entity_id"]) == str(top["id"])

    def test_a_record_with_no_parent_is_its_own_ultimate_parent(self, principal, parent):
        result = hierarchy.ultimate_parent_of(
            principal, "owned_by", "organization", str(parent["id"])
        )
        assert str(result["entity_id"]) == str(parent["id"])
        assert result["depth"] == 0
        assert hierarchy.ancestors_of(
            principal, "owned_by", "organization", str(parent["id"])
        ) == []

    def test_descendants_is_the_mirror_of_ancestors(self, principal, parent, child, grandchild):
        _link(principal, child["id"], parent["id"])
        _link(principal, grandchild["id"], child["id"])

        kids = hierarchy.descendants_of(principal, "owned_by", "organization", str(parent["id"]))
        ids = {str(k["entity_id"]) for k in kids}
        assert ids == {str(child["id"]), str(grandchild["id"])}

    def test_an_org_moved_between_parents_keeps_the_old_one_as_history(
        self, principal, parent, child,
    ):
        """R6's dated-associations discipline applied to the hierarchy itself:
        a restructuring is a new edge with a start date, not an overwrite --
        so the prior parent is still answerable as history, exactly like a
        person's `works_at` stint."""
        other_parent = repository.create(principal, "organization", {"name": "Other Holdco"})

        first = _link(principal, child["id"], parent["id"], valid_from="2020-01-01")
        associations.end_association(principal, str(first["id"]), on="2024-01-01")
        _link(principal, child["id"], other_parent["id"], valid_from="2024-01-01")

        current = hierarchy.ancestors_of(principal, "owned_by", "organization", str(child["id"]))
        assert [str(c["entity_id"]) for c in current] == [str(other_parent["id"])]

        historic = associations.edges_for(
            principal, "organization", str(child["id"]), include_history=True
        )
        assert len(historic) == 2

    def test_only_a_hierarchical_role_may_be_traversed(self, principal, parent):
        with pytest.raises(ValueError):
            hierarchy.ancestors_of(principal, "works_at", "organization", str(parent["id"]))


class TestCycleGuard:
    def test_a_self_loop_is_rejected(self, principal, parent):
        with pytest.raises(associations.AssociationError):
            _link(principal, parent["id"], parent["id"])

    def test_linking_a_parent_under_its_own_descendant_is_rejected(
        self, principal, parent, child, grandchild,
    ):
        """parent -> child -> grandchild already exists (as owned_by
        edges, child = from_id). Making `parent` owned by its own
        grandchild would close a three-node cycle -- must raise, not hang."""
        _link(principal, child["id"], parent["id"])
        _link(principal, grandchild["id"], child["id"])

        with pytest.raises(associations.AssociationError):
            _link(principal, parent["id"], grandchild["id"])

        # The graph is unharmed -- the rejected edge never landed.
        assert hierarchy.ancestors_of(
            principal, "owned_by", "organization", str(parent["id"])
        ) == []

    def test_a_direct_two_node_cycle_is_rejected(self, principal, parent, child):
        _link(principal, child["id"], parent["id"])
        with pytest.raises(associations.AssociationError):
            _link(principal, parent["id"], child["id"])

    def test_a_non_hierarchical_role_is_never_cycle_checked(self, principal):
        """Ordinary (non-hierarchical) roles never run the cycle guard --
        `co_investor_in` between the same two orgs in both directions is
        exactly what symmetric-role canonicalization (tested elsewhere)
        collapses to one row, not a rejected cycle."""
        a = repository.create(principal, "organization", {"name": "A Co"})
        b = repository.create(principal, "organization", {"name": "B Co"})
        associations.associate(
            principal, role="vendor_to", from_type="organization", from_id=str(a["id"]),
            to_type="organization", to_id=str(b["id"]),
        )
        # The reverse direction is a different (non-symmetric) relationship,
        # not a cycle -- must succeed.
        associations.associate(
            principal, role="vendor_to", from_type="organization", from_id=str(b["id"]),
            to_type="organization", to_id=str(a["id"]),
        )


class TestDepthCap:
    def test_a_chain_longer_than_max_depth_is_truncated_not_infinite(self, principal):
        """The depth cap is a second, independent guard from the write-time
        cycle check (hierarchy.py's own docstring: a direct SQL write bypasses
        the cycle check entirely). Exercised here via the explicit `max_depth`
        parameter rather than building a real 50-deep chain to prove the cap
        actually stops the walk instead of returning everything."""
        orgs = [
            repository.create(principal, "organization", {"name": f"Chain {i}"})
            for i in range(6)
        ]
        for i in range(len(orgs) - 1):
            _link(principal, orgs[i]["id"], orgs[i + 1]["id"])

        chain = hierarchy.ancestors_of(
            principal, "owned_by", "organization", str(orgs[0]["id"]), max_depth=3,
        )
        assert len(chain) == 3
