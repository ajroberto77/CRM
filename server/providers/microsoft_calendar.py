"""Microsoft Graph calendar adapter (R2's `<provider>_calendar.py` for the
calendar axis). Ported from Cal's `microsoft_calendar.py` -- this is
safety rule 7's origin.

## The local-day window (safety rule 7)

Graph's `calendarView` query params (`startDateTime`/`endDateTime`) are
always interpreted as UTC, regardless of the `Prefer: outlook.timezone`
header -- that header only affects how the RESPONSE BODY's own
`start`/`end` fields render, never the query window. Hardcoding a `Z`
suffix on local midnight therefore silently shifts the window by the
account's whole UTC offset. The fix -- convert the account's local
midnight to UTC explicitly, end of day as local-midnight +
`timedelta(days=1)` wall-clock arithmetic, so a DST-transition day
correctly spans 23 or 25 real hours, never a hardcoded 24 -- lives once
in `server/providers/calendar.py`'s `local_day_utc_window` (R1: this is
provider-neutral logic, not reforked per adapter). `google_calendar.py`
shares the exact same call.

## Create-vs-update heuristic

Word-overlap on titles, minus stopwords, also shared via
`calendar.py`'s `pick_update_target` -- a proposed event's title words
are compared against every existing event's title words, and a target is
picked only when exactly one existing event's words intersect (0 or 2+
matches is ambiguous, so it's `None` -- the caller falls back to create).

## PATCH, not PUT

Graph's events resource does a partial merge on `PATCH`; `PUT` is not the
supported verb for updating an existing event (confirmed against
Microsoft's own API reference, ported from Cal's own comment citing the
same finding).
"""
from __future__ import annotations

from typing import Any, Optional

from server.providers import calendar as cal_dispatch
from server.providers import microsoft_graph as gc


def _tz_header(tz_name: str) -> dict[str, str]:
    return {"Prefer": f'outlook.timezone="{tz_name}"'}


def _calendar_path(account: dict[str, Any], suffix: str) -> str:
    calendar_id = account.get("default_calendar_id") or ""
    if calendar_id:
        return f"/me/calendars/{calendar_id}/{suffix}"
    return f"/me/{suffix}"


def check_event(
    org_id: str, account: dict[str, Any], date: str, proposed_title: Optional[str] = None
) -> dict[str, Any]:
    tz_name, start, end = cal_dispatch.local_day_utc_window(account, date)

    account_id = str(account["id"])
    path = _calendar_path(account, "calendarView")
    url = f"{gc.GRAPH_BASE}{path}?startDateTime={start}&endDateTime={end}"
    data = gc.request("GET", url, org_id, account_id, extra_headers=_tz_header(tz_name))
    existing = data.get("value", [])
    return {
        "existing_events": existing,
        "update_target": cal_dispatch.pick_update_target(
            proposed_title, existing, title_field="subject"
        ),
    }


def _event_body(
    tz_name: str, *, title: str, start_iso: str, end_iso: str, location: str = "",
    body_text: str = "", attendees: Optional[list[str]] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "subject": title,
        "start": {"dateTime": start_iso, "timeZone": tz_name},
        "end": {"dateTime": end_iso, "timeZone": tz_name},
    }
    if location:
        body["location"] = {"displayName": location}
    if body_text:
        body["body"] = {"contentType": "text", "content": body_text}
    if attendees:
        # Only meaningful when this account is the real send identity --
        # Graph sends a real meeting invite to each address listed here.
        body["attendees"] = [
            {"emailAddress": {"address": a}, "type": "required"} for a in attendees
        ]
    return body


def create_event(
    org_id: str, account: dict[str, Any], *, title: str, start_iso: str, end_iso: str,
    location: str = "", body_text: str = "", attendees: Optional[list[str]] = None,
) -> str:
    tz_name, _ = cal_dispatch.account_timezone(account)
    account_id = str(account["id"])
    path = _calendar_path(account, "events")
    body = _event_body(
        tz_name, title=title, start_iso=start_iso, end_iso=end_iso,
        location=location, body_text=body_text, attendees=attendees,
    )
    result = gc.request("POST", path, org_id, account_id, body=body, extra_headers=_tz_header(tz_name))
    return result["id"]


def update_event(
    org_id: str, account: dict[str, Any], provider_event_id: str, *, title: str,
    start_iso: str, end_iso: str, location: str = "", body_text: str = "",
    attendees: Optional[list[str]] = None,
) -> str:
    tz_name, _ = cal_dispatch.account_timezone(account)
    account_id = str(account["id"])
    body = _event_body(
        tz_name, title=title, start_iso=start_iso, end_iso=end_iso,
        location=location, body_text=body_text, attendees=attendees,
    )
    gc.request(
        "PATCH", f"/me/events/{provider_event_id}", org_id, account_id,
        body=body, extra_headers=_tz_header(tz_name),
    )
    return provider_event_id
