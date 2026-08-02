"""Installs whatever `modules/*` packages `config.get_enabled_modules()` names.

R6: core must never mention a fund, so this loader is the ONLY place a
module's package name is resolved, and it does so dynamically -- the actual
names come from config (a runtime string), never a literal `import
modules.funds` anywhere under `server/`. `tests/test_vertical_funds.py`'s
tokenized scan enforces exactly that.
"""
from __future__ import annotations

import importlib

from server import config
from server.core import (
    deal_activity, derivation, interaction_embeddings, registry, scheduling_pipeline,
)


def install_enabled_modules() -> None:
    """Register core, then let every enabled module contribute its entities,
    roles, validators and tables -- in that order, since a module's roles can
    reference core entities but not the reverse."""
    registry.register_core_entities()
    # Core behavior for core columns (person/organization.is_derived) -- not
    # a module, so it wires in here rather than through a modules/*/install().
    derivation.install()
    # M5: interaction -> scheduling extraction -> approval queue -> calendar
    # write. Also core, same reasoning.
    scheduling_pipeline.install()
    # M7: keeps core.deals.last_activity_at current -- the one signal deal
    # rotting (registry.py's deal entity) reads. Also core, same reasoning.
    deal_activity.install()
    # M7: interaction -> embed_interaction job -> ai.embeddings. Also core,
    # same reasoning.
    interaction_embeddings.install()
    for name in config.get_enabled_modules():
        module = importlib.import_module(f"modules.{name}")
        module.install()
