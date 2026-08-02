"""The investor portal module.

M9d built the write-time gate that closes the "executed subscription
document" gap. M9a adds investor classification: categories, pathways,
questionnaires and mandates. Offerings and the external identity class are
still designed-not-built -- see the milestone table (M9b/M9c) for what
remains.

`install()` is the whole integration surface, same shape as `modules/funds`:
schema first, then entities/roles/validators/subscribers. Nothing under
`server/core/` or `modules/funds` knows this module exists.
"""
from __future__ import annotations

from modules.investor_portal import manifest
from server.db import schema

MODULE = manifest.MODULE


def install() -> None:
    """Register the module's schema and entities. Idempotent."""
    schema.register_module_schema(manifest.TABLES, manifest.INDEXES)
    manifest.install()
