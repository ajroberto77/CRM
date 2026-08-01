"""The asset-management vertical.

`install()` is the whole integration surface: it contributes tables to the
migration and registers entities and association roles. Nothing under
`server/core/` knows this module exists.
"""
from __future__ import annotations

from modules.funds import manifest
from server.db import schema

MODULE = manifest.MODULE


def install() -> None:
    """Register the module's schema and entities. Idempotent."""
    schema.register_module_schema(manifest.TABLES, manifest.INDEXES)
    manifest.install()
