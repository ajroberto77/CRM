"""R3's calendar axis dispatcher.

Generic core, name-prefixed adapters (R2): this module carries no vendor
name and never imports a provider module except through the dispatch
functions below, each with its own lazy import -- matching
`server/providers/esign.py`'s and `contacts.py`'s established shape in
this codebase. Adding a third provider is one new `<provider>_calendar.py`
file implementing `check_event`/`create_event`/`update_event`, plus one
`elif` in each of the three functions here.

Contract ported from Cal's `calendar_provider.py`.

This module also carries the two pieces of logic every adapter needs
identically (R1 -- these were originally forked verbatim into both
`microsoft_calendar.py` and `google_calendar.py` and are collected here
instead): the account-timezone lookup, the local-day-to-UTC window
computation (safety rule 7), and the create-vs-update title-matching
heuristic. Each adapter's own file keeps only its provider-specific
request/response shape.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

PROVIDERS = ("microsoft", "google")

_STOPWORDS = {"the", "a", "an", "with", "and", "for", "at", "on", "to", "of", "in"}


class CalendarError(RuntimeError):
    pass


def account_timezone(account: dict[str, Any]) -> tuple[str, ZoneInfo]:
    """The account's stored IANA timezone name and its ZoneInfo. Falls
    back to UTC for a missing or invalid stored name -- never crashes."""
    tz_name = account.get("timezone") or "UTC"
    try:
        return tz_name, ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 -- an invalid stored tz name falls back, never crashes
        return "UTC", ZoneInfo("UTC")


def local_day_utc_window(account: dict[str, Any], date: str) -> tuple[str, str, str]:
    """Safety rule 7: a "local day" window computed via wall-clock
    arithmetic on an aware datetime, never a hardcoded 24-hour span, so a
    DST-transition day correctly spans 23 or 25 real hours. Returns
    `(tz_name, start_utc_iso, end_utc_iso)`."""
    tz_name, tz = account_timezone(account)
    local_start = datetime.fromisoformat(date).replace(tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    start = local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = local_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return tz_name, start, end


def _title_words(title: Optional[str]) -> set[str]:
    if not title:
        return set()
    return {w.strip(".,!?").lower() for w in title.split()} - _STOPWORDS


def pick_update_target(
    proposed_title: Optional[str], existing_events: list[dict[str, Any]], *, title_field: str
) -> Optional[dict[str, Any]]:
    """Word-overlap-on-titles-minus-stopwords, shared by every adapter: a
    proposed event's title words are compared against each existing
    event's title words (read from `title_field`, since Graph calls it
    "subject" and Google calls it "summary"), and a target is picked only
    when exactly one existing event's words intersect -- 0 or 2+ matches
    is ambiguous, so it's None and the caller falls back to create."""
    proposed_words = _title_words(proposed_title)
    if not proposed_words:
        return None
    matches = [
        e for e in existing_events if _title_words(e.get(title_field)) & proposed_words
    ]
    return matches[0] if len(matches) == 1 else None


def check_event(
    org_id: str, account: dict[str, Any], date: str, proposed_title: Optional[str] = None
) -> dict[str, Any]:
    """Fetches the account's local-day event list and, if `proposed_title`
    is given, a single unambiguous existing event it might be an update
    to (see each adapter's title-word-overlap heuristic)."""
    provider = account["provider"]
    if provider == "microsoft":
        import server.providers.microsoft_calendar as impl
    elif provider == "google":
        import server.providers.google_calendar as impl
    else:
        raise CalendarError(
            f"unknown calendar provider {provider!r} (known: {', '.join(PROVIDERS)})"
        )
    return impl.check_event(org_id, account, date, proposed_title)


def create_event(org_id: str, account: dict[str, Any], **kwargs: Any) -> str:
    provider = account["provider"]
    if provider == "microsoft":
        import server.providers.microsoft_calendar as impl
    elif provider == "google":
        import server.providers.google_calendar as impl
    else:
        raise CalendarError(
            f"unknown calendar provider {provider!r} (known: {', '.join(PROVIDERS)})"
        )
    return impl.create_event(org_id, account, **kwargs)


def update_event(
    org_id: str, account: dict[str, Any], provider_event_id: str, **kwargs: Any
) -> str:
    provider = account["provider"]
    if provider == "microsoft":
        import server.providers.microsoft_calendar as impl
    elif provider == "google":
        import server.providers.google_calendar as impl
    else:
        raise CalendarError(
            f"unknown calendar provider {provider!r} (known: {', '.join(PROVIDERS)})"
        )
    return impl.update_event(org_id, account, provider_event_id, **kwargs)
