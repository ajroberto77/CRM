"""Per-signer tracking for a document sent for e-signature.

`core.document_signers` is not a registered entity -- same treatment as
`core.associations`: an internal detail of a document's signing workflow, not
a thing a user lists or filters generically. This module owns its SQL, the
same "a small number of modules own SQL, everything else calls their
functions" discipline `server/core/users.py` uses.

The document itself (`core.documents`) IS a registered entity and goes through
`server/core/repository.py` like any other record -- only the per-signer rows
live here.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from server.core.permissions import Principal
from server.db import pool


def add_signer(
    principal: Principal,
    document_id: str,
    *,
    subject_type: str,
    subject_id: str,
    role: str,
    order_index: int = 0,
) -> dict[str, Any]:
    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id) as cur:
        cur.execute(
            "INSERT INTO core.document_signers "
            "  (id, org_id, document_id, subject_type, subject_id, role, order_index) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (str(uuid.uuid4()), principal.org_id, document_id, subject_type,
             str(subject_id), role, order_index),
        )
        return dict(cur.fetchone())


def list_signers(principal: Principal, document_id: str) -> list[dict[str, Any]]:
    with pool.transaction(
        org_id=principal.org_id, user_id=principal.user_id, readonly=True
    ) as cur:
        cur.execute(
            "SELECT * FROM core.document_signers "
            "WHERE document_id = %s ORDER BY order_index, created_at",
            (document_id,),
        )
        return [dict(r) for r in cur.fetchall()]


_VALID_STATUSES = ("pending", "sent", "viewed", "signed", "declined")


def update_signer_status(
    principal: Principal,
    signer_id: str,
    status: str,
    *,
    provider_signer_id: Optional[str] = None,
    signed_at: Optional[Any] = None,
) -> dict[str, Any]:
    """Update one signer's status. Called by the esign provider's status-poll
    or webhook handler (M9d), never by a user directly -- so this takes a
    system principal in practice, not user input."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"unknown signer status {status!r}")
    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id) as cur:
        assignments = ["status = %s"]
        params: list[Any] = [status]
        if provider_signer_id is not None:
            assignments.append("provider_signer_id = %s")
            params.append(provider_signer_id)
        if signed_at is not None:
            assignments.append("signed_at = %s")
            params.append(signed_at)
        params.append(signer_id)
        cur.execute(
            f"UPDATE core.document_signers SET {', '.join(assignments)} "
            f"WHERE id = %s RETURNING *",
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"no document signer {signer_id}")
        return dict(row)


def all_signed(principal: Principal, document_id: str) -> bool:
    """True when every signer on this document has signed, and there is at
    least one signer. An empty signer list is not 'all signed' -- a document
    nobody was ever asked to sign is not executed."""
    signers = list_signers(principal, document_id)
    return bool(signers) and all(s["status"] == "signed" for s in signers)
