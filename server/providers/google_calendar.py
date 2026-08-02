"""Google Calendar API adapter. New work -- Cal has no Google calendar
code (Cal is Microsoft-only). The local-day->UTC window computation
(safety rule 7) and the create-vs-update title-matching heuristic are
provider-neutral and live once in `server/providers/calendar.py`
(`local_day_utc_window`/`pick_update_target`), not reforked here -- this
file only supplies Google's own request/response shape: `timeMin`/
`timeMax` RFC3339 query params, and `summary` (Google's field name for an
event's title, vs. Graph's `subject`) passed as `pick_update_target`'s
`title_field`.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Optional

from server.providers import calendar as cal_dispatch
from server.providers import google_api as ga

_BASE = "https://www.googleapis.com/calendar/v3"


def _calendar_id(account: dict[str, Any]) -> str:
    return account.get("default_calendar_id") or "primary"


def _events_url(account: dict[str, Any], *segments: str, **params: str) -> str:
    calendar_id = urllib.parse.quote(_calendar_id(account), safe="")
    path = "/".join(["", "calendars", calendar_id, "events", *segments])
    url = f"{_BASE}{path}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    return url


def check_event(
    org_id: str, account: dict[str, Any], date: str, proposed_title: Optional[str] = None
) -> dict[str, Any]:
    _tz_name, time_min, time_max = cal_dispatch.local_day_utc_window(account, date)

    account_id = str(account["id"])
    url = _events_url(
        account, timeMin=time_min, timeMax=time_max, singleEvents="true", orderBy="startTime",
    )
    data = ga.request("GET", url, org_id, account_id)
    existing = data.get("items", [])
    return {
        "existing_events": existing,
        "update_target": cal_dispatch.pick_update_target(
            proposed_title, existing, title_field="summary"
        ),
    }


def _event_body(
    tz_name: str, *, title: str, start_iso: str, end_iso: str, location: str = "",
    body_text: str = "", attendees: Optional[list[str]] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start_iso, "timeZone": tz_name},
        "end": {"dateTime": end_iso, "timeZone": tz_name},
    }
    if location:
        body["location"] = location
    if body_text:
        body["description"] = body_text
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    return body


def create_event(
    org_id: str, account: dict[str, Any], *, title: str, start_iso: str, end_iso: str,
    location: str = "", body_text: str = "", attendees: Optional[list[str]] = None,
) -> str:
    tz_name, _ = cal_dispatch.account_timezone(account)
    account_id = str(account["id"])
    body = _event_body(
        tz_name, title=title, start_iso=start_iso, end_iso=end_iso,
        location=location, body_text=body_text, attendees=attendees,
    )
    result = ga.request("POST", _events_url(account), org_id, account_id, body=body)
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
    url = _events_url(account, urllib.parse.quote(provider_event_id, safe=""))
    result = ga.request("PATCH", url, org_id, account_id, body=body)
    return result["id"]
