"""The one definition of "trusted" (CLAUDE.md's directory layout already
names this file). Ported from Cal's `senders.py`/`trusted_senders.py`,
generalized past calendar -- M6's message-channel auto-approve reuses this
directly rather than a second trust check.

Two sources, checked in order:

1. A specific person's `core.persons.auto_accept` flag, matched through
   `contact_channels` (M2's matching surface -- `persons.primary_email` is
   display denormalization only, per that column's own comment in
   `server/db/schema.py`, and is never matched against here). Attribution
   `"contact:<name>"`.
2. `core.trusted_senders`, matching either the exact email or its
   `"@domain"` pattern. `ORDER BY pattern DESC` makes an exact address
   sort after its own domain (`"j@x.com" > "@x.com"` lexicographically),
   so when both are trusted the more specific exact-address attribution
   wins. Attribution `"sender:<pattern>"`.

A blank or unparseable email always returns `None` -- safety rule 8: a
null/unrecoverable input must never auto-match anything.
"""
from __future__ import annotations

from typing import Optional

from server.core import identity, permissions, repository

# Trusting one of these as a whole domain would trust the entire public
# internet. Not a hard block -- a caller (e.g. a settings UI) can surface
# this as a confirmation prompt; this module only exposes the predicate.
PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "gmx.com", "mail.com", "zoho.com",
    "yandex.com", "fastmail.com", "hey.com", "qq.com", "163.com",
})


def is_public_domain(pattern: str) -> bool:
    return pattern.startswith("@") and pattern[1:].lower() in PUBLIC_EMAIL_DOMAINS


def normalize_pattern(raw: str) -> str:
    """A full email address, or an "@domain" (a bare "domain.com" is
    accepted and gets the "@" added). Raises ValueError for anything else.
    Called once at write time (`add_sender`) -- `trust_reason` never
    re-normalizes a stored pattern at read time.

    A full-address pattern is routed through `identity.normalize_email`
    (R5) rather than a local `.strip().lower()` -- `trust_reason` matches
    against `contact_channels.value_normalized`, which is itself written
    via `identity.normalize_email`, so both sides of that comparison must
    go through the exact same normalizer or a unicode-homoglyph or
    "Name <addr>"-wrapped address could match one table but not the
    other."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("a trusted-sender pattern is required")
    if value.startswith("@"):
        domain = value[1:].strip().lower()
        if not domain or "@" in domain or "." not in domain:
            raise ValueError(f"not a valid domain pattern: {raw!r}")
        return f"@{domain}"
    if "@" not in value:
        domain = value.lower()
        if "." not in domain:
            raise ValueError(f"not a valid email address or domain pattern: {raw!r}")
        return f"@{domain}"  # a bare domain typed without the leading '@'
    normalized = identity.normalize_email(value)
    if normalized is None:
        raise ValueError(f"not a valid email address or domain pattern: {raw!r}")
    return normalized


def add_sender(principal, pattern: str, *, label: str = "", note: str = "") -> dict:
    """Upsert -- re-adding an existing pattern updates its label/note
    rather than raising. Trust isn't additive across two calls with
    different labels; the latest call wins."""
    normalized = normalize_pattern(pattern)
    existing = repository.list_records(
        principal, "trusted_sender",
        filters={"field": "pattern", "op": "eq", "value": normalized}, limit=1,
    )
    if existing["total"] > 0:
        record_id = str(existing["records"][0]["id"])
        return repository.update(
            principal, "trusted_sender", record_id, {"label": label, "note": note}
        )
    return repository.create(
        principal, "trusted_sender", {"pattern": normalized, "label": label, "note": note}
    )


def trust_reason(org_id: str, email: Optional[str]) -> Optional[str]:
    normalized = identity.normalize_email(email)
    if not normalized:
        return None
    principal = permissions.system_principal(org_id, "trust check")

    channels = repository.list_records(
        principal, "contact_channel",
        filters={"and": [
            {"field": "kind", "op": "eq", "value": "email"},
            {"field": "value_normalized", "op": "eq", "value": normalized},
        ]},
        limit=1,
    )
    if channels["total"] > 0:
        person_id = str(channels["records"][0]["person_id"])
        person = repository.get_record(principal, "person", person_id)
        if person.get("auto_accept"):
            return f"contact:{person.get('full_name') or normalized}"

    domain = "@" + (identity.email_domain(normalized) or "")
    senders = repository.list_records(
        principal, "trusted_sender",
        filters={"field": "pattern", "op": "in", "value": [normalized, domain]},
        sort=[{"field": "pattern", "direction": "desc"}],
        limit=1,
    )
    if senders["total"] > 0:
        return f"sender:{senders['records'][0]['pattern']}"

    return None


def is_trusted(org_id: str, email: Optional[str]) -> bool:
    return trust_reason(org_id, email) is not None
