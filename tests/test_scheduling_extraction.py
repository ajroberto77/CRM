"""server/extraction/scheduling.py -- the field validators are safety
rule 6's origin (the attendees-string regression especially), and the
full extract_scheduling_info() path exercises M3's repair-retry via a
mocked LLM adapter call, never a real network call.
"""
from __future__ import annotations

from unittest import mock

import pytest

from server.core import users
from server.extraction import scheduling
from server.llm import ollama_llm


class TestValidateAttendees:
    def test_a_bare_string_is_rejected_not_list_wrapped(self):
        """The exact safety-rule-6 bug: list("a@b.com") explodes into one
        attendee per character."""
        with pytest.raises(ValueError):
            scheduling._validate_attendees("a@b.com")

    def test_a_list_of_strings_is_accepted(self):
        assert scheduling._validate_attendees(["a@b.com", "c@d.com"]) == ["a@b.com", "c@d.com"]

    def test_none_becomes_an_empty_list(self):
        assert scheduling._validate_attendees(None) == []

    def test_a_non_string_item_is_rejected(self):
        with pytest.raises(ValueError):
            scheduling._validate_attendees(["a@b.com", 42])


class TestValidateTime:
    def test_a_range_is_rejected(self):
        with pytest.raises(ValueError):
            scheduling._validate_time("14:00:00 - 15:00:00")

    def test_valid_time(self):
        assert scheduling._validate_time("14:30") == "14:30"

    def test_none_is_allowed(self):
        assert scheduling._validate_time(None) is None

    def test_out_of_range_hour_is_rejected(self):
        with pytest.raises(ValueError):
            scheduling._validate_time("25:00")


class TestValidateDate:
    def test_a_fake_calendar_date_is_rejected(self):
        with pytest.raises(ValueError):
            scheduling._validate_date("2026-13-45")

    def test_valid_date(self):
        assert scheduling._validate_date("2026-03-10") == "2026-03-10"

    def test_malformed_shape_is_rejected(self):
        with pytest.raises(ValueError):
            scheduling._validate_date("March 10th")


class TestValidateDuration:
    def test_a_bool_is_rejected_even_though_bool_is_an_int_subclass(self):
        with pytest.raises(ValueError):
            scheduling._validate_duration(True)

    def test_a_positive_int_is_accepted(self):
        assert scheduling._validate_duration(30) == 30

    def test_a_numeric_string_is_coerced(self):
        assert scheduling._validate_duration("45") == 45

    def test_a_non_numeric_string_is_rejected(self):
        with pytest.raises(ValueError):
            scheduling._validate_duration("an hour")

    def test_zero_or_negative_is_rejected(self):
        with pytest.raises(ValueError):
            scheduling._validate_duration(0)
        with pytest.raises(ValueError):
            scheduling._validate_duration(-5)


class TestValidateCategory:
    def test_none_is_allowed_and_fails_safe(self):
        """An unclassifiable event must stay uncategorized, not be forced
        into a bucket that might match the auto-accept rule (safety rule 8)."""
        assert scheduling._validate_category(None) is None

    def test_a_closed_vocabulary_value_is_accepted(self):
        assert scheduling._validate_category("medical") == "medical"

    def test_an_unknown_category_is_rejected(self):
        with pytest.raises(ValueError):
            scheduling._validate_category("astrology")


class TestStripQuotedReply:
    def test_strips_below_an_on_wrote_boundary(self):
        text = "New time works: 3pm.\n\nOn Mon, Jan 1 wrote:\n> old stuff\n> 2pm original"
        assert scheduling._strip_quoted_reply(text) == "New time works: 3pm."

    def test_no_boundary_returns_the_full_text(self):
        text = "Just a plain message, no quote."
        assert scheduling._strip_quoted_reply(text) == text

    def test_an_empty_above_section_falls_back_to_the_full_text(self):
        text = "On Mon, Jan 1 wrote:\n> only quoted content"
        assert scheduling._strip_quoted_reply(text) == text


class TestValidateExtraction:
    def test_full_valid_payload(self):
        result = scheduling._validate_extraction({
            "is_scheduling_relevant": True,
            "events": [{"title": "Dentist", "date": "2026-03-10", "time": "09:00", "confidence": 0.9}],
        })
        assert result["is_scheduling_relevant"] is True
        assert result["events"][0]["title"] == "Dentist"

    def test_missing_is_scheduling_relevant_raises(self):
        with pytest.raises(ValueError):
            scheduling._validate_extraction({"events": []})

    def test_events_must_be_a_list(self):
        with pytest.raises(ValueError):
            scheduling._validate_extraction({"is_scheduling_relevant": True, "events": "not a list"})


class TestExtractSchedulingInfo:
    def test_calls_the_llm_router_and_returns_the_validated_result(self, org_id):
        fake_response = {
            "is_scheduling_relevant": True,
            "events": [{
                "title": "Dentist", "date": "2026-03-10", "time": "09:00", "duration_minutes": 30,
                "location": None, "attendees": [], "note": None, "category": "medical",
                "confidence": 0.92,
            }],
        }
        with mock.patch.object(ollama_llm, "call_structured", return_value=fake_response):
            result = scheduling.extract_scheduling_info(org_id, "Dentist appointment March 10 at 9am?")
        assert result["events"][0]["title"] == "Dentist"

    def test_a_malformed_first_response_gets_repaired_via_the_router(self, org_id):
        calls = {"n": 0}

        def flaky(prompt, schema, *, model, settings, temperature=None, max_tokens=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"is_scheduling_relevant": True, "events": [{"attendees": "bare@string.com", "confidence": 0.5}]}
            return {"is_scheduling_relevant": True, "events": [{"confidence": 0.5}]}

        with mock.patch.object(ollama_llm, "call_structured", side_effect=flaky):
            result = scheduling.extract_scheduling_info(org_id, "reschedule please")
        assert calls["n"] == 2
        assert result["events"][0]["confidence"] == 0.5

    def test_reference_date_is_included_in_the_prompt(self, org_id, monkeypatch):
        captured = {}

        def fake_extract_structured(org_id_arg, prompt, schema, *, validate=None):
            captured["prompt"] = prompt
            return validate({"is_scheduling_relevant": False, "events": []})

        monkeypatch.setattr(scheduling.llm_router, "extract_structured", fake_extract_structured)
        scheduling.extract_scheduling_info(org_id, "this Wednesday works", reference_date="2026-03-09")
        assert "2026-03-09" in captured["prompt"]
        assert "Monday" in captured["prompt"]
