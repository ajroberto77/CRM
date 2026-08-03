"""The one normalizer. These are the cases that decide whether two channels
resolve to one person, so they are worth being explicit about."""
from __future__ import annotations

import pytest

from server.core import identity


class TestEmail:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Bob@Acme.COM", "bob@acme.com"),
            ("  bob@acme.com  ", "bob@acme.com"),
            ("Bob Smith <Bob@Acme.com>", "bob@acme.com"),
            ('"Bob" <bob@acme.com>', "bob@acme.com"),
            ("BOB@ACME.CO.UK", "bob@acme.co.uk"),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert identity.normalize_email(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "not-an-email", "no@tld", "a b@c.com", "@acme.com"])
    def test_rejects(self, raw):
        assert identity.normalize_email(raw) is None

    def test_plus_tags_and_dots_are_preserved(self):
        """Collapsing them would merge two people permanently. A CRM may offer
        that as an explicit merge; it must never guess it."""
        assert identity.normalize_email("bob+crm@acme.com") == "bob+crm@acme.com"
        assert identity.normalize_email("b.o.b@acme.com") == "b.o.b@acme.com"
        assert not identity.same("email", "bob+a@acme.com", "bob@acme.com")

    def test_domain(self):
        assert identity.email_domain("Bob@Acme.com") == "acme.com"
        assert identity.email_domain("nonsense") is None


class TestPhone:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("+1 (555) 010-0123", "+15550100123"),
            ("555-010-0123", "+15550100123"),
            ("5550100123", "+15550100123"),
            ("+44 20 7946 0000", "+442079460000"),
            ("00 44 20 7946 0000", "+442079460000"),
            ("1-555-010-0123", "+15550100123"),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert identity.normalize_phone(raw) == expected

    def test_all_formats_of_one_number_collapse(self):
        forms = ["+1 555 010 0123", "(555) 010-0123", "555.010.0123", "+15550100123"]
        assert len({identity.normalize_phone(f) for f in forms}) == 1

    def test_extension_is_stripped_not_absorbed(self):
        assert identity.normalize_phone("555-010-0123 x99") == "+15550100123"
        assert identity.normalize_phone("555-010-0123 ext. 99") == "+15550100123"

    @pytest.mark.parametrize("raw", ["", None, "12345", "abc", "+" + "9" * 20])
    def test_rejects(self, raw):
        assert identity.normalize_phone(raw) is None

    def test_country_code_not_doubled(self):
        assert identity.normalize_phone("15550100123") == "+15550100123"


class TestHandles:
    @pytest.mark.parametrize(
        "raw,expected",
        [("@Bob_Smith", "bob_smith"), ("bob_smith", "bob_smith"), ("123456789", "123456789")],
    )
    def test_telegram(self, raw, expected):
        assert identity.normalize_telegram(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "way_too_long_" * 5, "has spaces", "bad-dash"])
    def test_telegram_rejects(self, raw):
        assert identity.normalize_telegram(raw) is None

    def test_signal_is_a_phone_number(self):
        assert identity.normalize_signal("(555) 010-0123") == "+15550100123"


class TestDispatch:
    def test_unknown_kind_raises_rather_than_returning_none(self):
        """A typo'd kind must fail loudly. Returning None would silently store
        an unmatched value and quietly split a person in two."""
        with pytest.raises(identity.NormalizationError):
            identity.normalize("emial", "bob@acme.com")

    def test_normalize_required_raises_on_unparseable(self):
        with pytest.raises(identity.NormalizationError):
            identity.normalize_required("email", "nonsense")

    def test_same_is_false_when_either_side_is_unparseable(self):
        assert not identity.same("email", "nonsense", "nonsense")

    @pytest.mark.parametrize("kind", identity.CHANNEL_KINDS)
    def test_every_declared_kind_has_a_normalizer(self, kind):
        assert identity.normalize(kind, "") is None


def test_name_normalization_collapses_whitespace_and_case():
    assert identity.normalize_name("  Bob   Smith ") == "bob smith"
    assert identity.normalize_name("BOB SMITH") == identity.normalize_name("bob smith")


class TestCountry:
    @pytest.mark.parametrize(
        "raw,expected",
        [("us", "US"), (" gb ", "GB"), ("De", "DE"), ("JP", "JP")],
    )
    def test_normalizes(self, raw, expected):
        assert identity.normalize_country(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "USA", "United States", "Freedonia", "XX"])
    def test_rejects(self, raw):
        """Alpha-3 codes and full names are deliberately not accepted -- one
        canonical form, not a second one that has to be kept in sync."""
        assert identity.normalize_country(raw) is None
