"""The investor portal module.

This is a deliberately minimal first slice: the write-time gate that closes
the "executed subscription document" gap identified in
`docs/INVESTOR-PORTAL.md`. Investor categories, pathways, questionnaires,
mandates, offerings and the external identity class are designed there but not
built here -- see the milestone table (M9a-d) for what is still to come.

`install()` is the whole integration surface, same shape as `modules/funds`.
Nothing under `server/core/` or `modules/funds` knows this module exists.
"""
from __future__ import annotations

from modules.investor_portal import manifest

MODULE = manifest.MODULE


def install() -> None:
    """Register the module's validators. Idempotent."""
    manifest.install()
