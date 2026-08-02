"""Per-org, hot-reloaded product settings (R5 -- the one place these live).

`server/config.py`'s own docstring names this file as the intended home for
settings that are distinct from environment-only infrastructure config:
"Product settings (approval mode, LLM provider, sync cadence) live in
Postgres, per org, and are hot-reloaded. They are NOT here -- see
server/core/settings.py."

`section` is a small closed vocabulary (`"llm"`, `"approval"`, `"pipeline"`)
rather than one row per individual key -- a whole related group reads and
writes together,
which is what makes `update_settings()`'s read-merge-write discipline work:
there is exactly one row to lock, merge into, and write back per section,
never a scatter of independent single-key writes that could race or
partially apply.

## The one thing this module exists to prevent

A sibling project's settings-save handler loaded a SUBSET of keys from a
form submission and wrote it back as the WHOLE settings file, silently
blanking every key the submitted form didn't happen to include -- 15 LLM
provider keys destroyed on one unrelated save (docs/SOURCE-PATTERNS.md,
CLAUDE.md safety rule 2). `update_settings()` is structurally incapable of
that failure: it always reads the current row inside the same transaction
it writes in, merges `changes` over it, and writes the merged whole back --
there is no code path here that writes anything but the full merged dict.

## Layering

Like `server/core/users.py`, this is a trusted internal module that takes a
raw `org_id`, not a `Principal` -- it does not itself decide who may read or
write a section. Authorization (today: admin-only) is the caller's job, at
the API layer (`server/api/settings.py`), the same split `users.py` already
has with the routes that call it.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from server.db import pool

# The closed set of settings sections. Adding one is a one-line change here;
# nothing else needs to know the list grew.
SECTIONS = ("llm", "approval", "pipeline")


class UnknownSection(ValueError):
    pass


def _check_section(section: str) -> None:
    if section not in SECTIONS:
        raise UnknownSection(
            f"unknown settings section {section!r} (known: {', '.join(SECTIONS)})"
        )


def get_settings(org_id: str, section: str) -> dict[str, Any]:
    """This org's settings for `section`, or `{}` if never saved -- never
    `None`, so a caller can always `.get()` a key without a null check."""
    _check_section(section)
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute(
            "SELECT values FROM core.settings WHERE org_id = %s AND section = %s",
            (org_id, section),
        )
        row = cur.fetchone()
        return dict(row["values"]) if row else {}


def update_settings(
    org_id: str,
    section: str,
    changes: dict[str, Any],
    *,
    clear: Iterable[str] = (),
    updated_by: Optional[str] = None,
) -> dict[str, Any]:
    """Read-merge-write, the row locked for the duration of one transaction
    -- the choke point safety rule 2 exists for. `clear` names keys to
    remove first; `changes` then merges shallowly over whatever remains.
    Returns the full merged section, matching what a subsequent
    `get_settings()` would return.
    """
    _check_section(section)
    with pool.transaction(org_id=org_id, user_id=updated_by) as cur:
        cur.execute(
            "SELECT id, values FROM core.settings WHERE org_id = %s AND section = %s "
            "FOR UPDATE",
            (org_id, section),
        )
        row = cur.fetchone()
        current = dict(row["values"]) if row else {}
        for key in clear:
            current.pop(key, None)
        current.update(changes)
        payload = json.dumps(current, default=str)

        if row:
            cur.execute(
                "UPDATE core.settings SET values = %s::jsonb, updated_at = now(), "
                "updated_by = %s WHERE id = %s",
                (payload, updated_by, row["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO core.settings (id, org_id, section, values, updated_by) "
                "VALUES (gen_random_uuid(), %s, %s, %s::jsonb, %s)",
                (org_id, section, payload, updated_by),
            )
        return current
