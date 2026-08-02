"""Scheduling extraction -- the first real task built on
`server/llm/router.py`'s `extract_structured()`. Ported from Cal's
`scheduling_extraction.py`, including the exact field validators that are
safety rule 6's origin.

The router stays task-agnostic (`docs/DESIGN.md`'s own split): this file
owns its prompt, its schema, and its validation -- a second extraction
task (M9a-style classification, or a future one) gets its own file here,
never a branch inside `router.py`.

## Why every optional field validates strictly rather than coercing

Each guard below corresponds to a real defect that reached calendar
writes in Cal:

- `_validate_attendees` rejects a bare string outright rather than
  `list()`-wrapping it -- a bare string is truthy, so it used to flow
  straight through, and `list("a@b.com")` explodes into one attendee PER
  CHARACTER, each sent to a calendar provider as a required attendee on a
  real invite (safety rule 6, verbatim).
- `_validate_time` rejects a *range* like `"14:00:00 - 15:00:00"`, which
  once reached calendar creation unchecked.
- `_validate_date` parses with `datetime.strptime`, not a regex alone, so
  a well-formed-but-fake date (`"2026-13-45"`) is rejected rather than
  silently reinterpreted.
- `_validate_category` allows `None` deliberately -- an unclassifiable
  event must fail safe to "no category," which the auto-accept category
  rule can never match, rather than being forced into a bucket that might
  be on the auto-accept list (safety rule 8).

Every raised `ValueError` here is exactly what `extract_structured`'s
repair-retry mechanism is built to catch -- one same-provider retry with a
hint, never a silent coercion.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from server.llm import router as llm_router

EVENT_CATEGORIES = (
    "school", "sports", "medical", "appointment", "social", "work", "travel", "other",
)

EVENT_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_scheduling_relevant": {"type": "boolean"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": ["string", "null"]},
                    "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                    "time": {
                        "type": ["string", "null"],
                        "description": "HH:MM 24h, null means all-day",
                    },
                    "duration_minutes": {"type": ["integer", "null"]},
                    "location": {"type": ["string", "null"]},
                    "attendees": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": ["string", "null"]},
                    "category": {"type": ["string", "null"], "enum": [*EVENT_CATEGORIES, None]},
                    "confidence": {"type": "number"},
                },
                "required": ["confidence"],
            },
        },
    },
    "required": ["is_scheduling_relevant", "events"],
}

_EXTRACTION_PROMPT_TEMPLATE = """You are extracting scheduling information from an email.

Decide whether this email is scheduling-relevant (proposes, confirms, or
reschedules a specific event with a date/time) versus purely conversational.

If relevant, extract one entry per distinct event mentioned. For each
event, fill in every field you can determine with confidence; leave a
field null rather than guessing at a value you cannot support from the
text. `confidence` is your own estimate (0.0-1.0) of how sure you are this
extraction is correct.
{reference_block}
Email body:
---
{body}
---

Respond with a JSON object matching the schema exactly, nothing else."""

_REFERENCE_TEMPLATE = """
Today's date is {reference_date} ({reference_weekday}). Resolve any
relative date reference ("this Wednesday", "next Tuesday") against this
date -- look up the actual weekday, do not calculate it yourself.
"""

# Quoted-reply boundary patterns, earliest match wins. A reschedule reply's
# NEW time must not be shadowed by stale quoted text below it -- found live
# in Cal when a reply's actual new time was ignored in favor of an older
# quoted proposal further down the same email.
_QUOTE_BOUNDARY_PATTERNS = (
    re.compile(r"^On .+ wrote:\s*$", re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE),
    re.compile(r"^From:\s.+$", re.MULTILINE),
    re.compile(r"^>", re.MULTILINE),
)


def _strip_quoted_reply(text: str) -> str:
    earliest: Optional[int] = None
    for pattern in _QUOTE_BOUNDARY_PATTERNS:
        match = pattern.search(text)
        if match and (earliest is None or match.start() < earliest):
            earliest = match.start()
    if earliest is None:
        return text
    above = text[:earliest].strip()
    return above if above else text


def _validate_text(name: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name}: expected a string or null, got {type(value).__name__}")
    return value


def _validate_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise ValueError(f"date: {value!r} is not YYYY-MM-DD")
    from datetime import date as _date
    try:
        _date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"date: {value!r} is not a real calendar date") from exc
    return value


def _validate_time(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not re.match(r"^\d{2}:\d{2}$", value):
        raise ValueError(f"time: {value!r} is not HH:MM 24h (a range is not a time)")
    hour, minute = (int(part) for part in value.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time: {value!r} is out of range")
    return value


def _validate_duration(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("duration_minutes: a boolean is not a duration")
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        minutes = int(value)
    else:
        raise ValueError(f"duration_minutes: {value!r} is not a whole number of minutes")
    if minutes <= 0:
        raise ValueError(f"duration_minutes: {minutes} must be positive")
    return minutes


def _validate_attendees(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # The exact safety-rule-6 bug: a bare string must never be
        # list()-wrapped -- that explodes into one attendee per character.
        raise ValueError(
            f"attendees: expected a list of addresses, got a bare string {value!r}"
        )
    if not isinstance(value, list):
        raise ValueError(f"attendees: expected a list, got {type(value).__name__}")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"attendees: every entry must be a string, got {item!r}")
    return value


def _validate_category(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value not in EVENT_CATEGORIES:
        raise ValueError(f"category: {value!r} is not one of {EVENT_CATEGORIES}")
    return value


def _validate_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"confidence: expected a number, got {value!r}")
    number = float(value)
    if not (0.0 <= number <= 1.0):
        raise ValueError(f"confidence: {number} is outside 0.0-1.0")
    return number


def _validate_event(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"event: expected an object, got {type(raw).__name__}")
    return {
        "title": _validate_text("title", raw.get("title")),
        "date": _validate_date(raw.get("date")),
        "time": _validate_time(raw.get("time")),
        "duration_minutes": _validate_duration(raw.get("duration_minutes")),
        "location": _validate_text("location", raw.get("location")),
        "attendees": _validate_attendees(raw.get("attendees")),
        "note": _validate_text("note", raw.get("note")),
        "category": _validate_category(raw.get("category")),
        "confidence": _validate_confidence(raw.get("confidence")),
    }


def _validate_extraction(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
    if not isinstance(raw.get("is_scheduling_relevant"), bool):
        raise ValueError("is_scheduling_relevant: expected a boolean")
    events_raw = raw.get("events")
    if not isinstance(events_raw, list):
        raise ValueError(f"events: expected a list, got {type(events_raw).__name__}")
    return {
        "is_scheduling_relevant": raw["is_scheduling_relevant"],
        "events": [_validate_event(e) for e in events_raw],
    }


def extract_scheduling_info(
    org_id: str, email_text: str, *, reference_date: Optional[str] = None
) -> dict[str, Any]:
    body = _strip_quoted_reply(email_text or "")
    reference_block = ""
    if reference_date:
        from datetime import date as _date
        weekday = _date.fromisoformat(reference_date).strftime("%A")
        reference_block = _REFERENCE_TEMPLATE.format(
            reference_date=reference_date, reference_weekday=weekday
        )
    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(reference_block=reference_block, body=body)
    return llm_router.extract_structured(
        org_id, prompt, EVENT_EXTRACTION_SCHEMA, validate=_validate_extraction
    )
