"""The one place an ISO date string is parsed and a `valid_from`/`valid_to`
overlap window becomes SQL.

Pure stdlib, with no import of any other `server.core` module, by design:
`associations.py` (-> `repository.py` -> `query.py`) and `query.py` both need
this, and `query.py` cannot import `associations.py` without closing that
cycle. A dependency-free leaf both already sit above is what lets the same
logic be written once instead of twice (R1) — see `identity.py`'s
`normalize_cik` docstring for why a value normalized in more than one place
eventually normalizes two slightly different ways.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional


class DateParseError(ValueError):
    """A value that cannot be parsed as an ISO date. Callers wrap this in
    their own domain exception (`query.FilterError`, `associations.
    AssociationError`) rather than letting it escape as-is, so each module's
    public exception contract stays whatever it already was."""


def parse_date(value: Any, label: str, *, empty_is_none: bool = True) -> Optional[date]:
    """A `date` passes through unchanged; a `datetime` or an ISO 8601 string
    is parsed; anything else raises `DateParseError`.

    `empty_is_none` covers the one real difference between this function's
    two shapes of caller: an `as_of`/`valid_from` argument treats `None`/`""`
    as "not given" (`associations._parse_date`'s original behavior), while an
    ordinary filter VALUE (`query._coerce_scalar`'s `date`-kind branch) must
    still reject an explicit empty string as a bad value, not silently treat
    it as absent.
    """
    if value is None or (empty_is_none and value == ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise DateParseError(f"{label}: {value!r} is not an ISO date") from exc


def overlap_clause(
    *, include_history: bool, as_of: Optional[date], alias: str = "",
) -> tuple[str, list[Any]]:
    """SQL (and bound params) matching a dated row whose `[valid_from,
    valid_to]` window covers `as_of` (defaulting to today), or a literal
    `TRUE` when `include_history` -- shared by `associations.edges_for()`
    (queried column-bare, no `alias`) and `query._compile_role_clause()`
    (queried through an aliased `EXISTS` subquery).
    """
    if include_history:
        return "TRUE", []
    when = as_of or date.today()
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}valid_from IS NULL OR {prefix}valid_from <= %s) "
        f"AND ({prefix}valid_to IS NULL OR {prefix}valid_to >= %s)",
        [when, when],
    )
