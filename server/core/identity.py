"""THE normalizer (R5). Email, phone and messaging-handle canonicalization.

Import this. Never reimplement it, and never inline a `.strip().lower()` on a
value that has a normalizer here -- not even when the inline version is locally
correct. That discipline is CATO's `normalize_cik` lesson, learned there the
hard way: a value normalized in nine places is a value normalized nine slightly
different ways, and the ninth is a bug nobody finds for months.

This matters more here than it did there. `contact_channels` is unique on
`(kind, value_normalized)`, and it is the table that makes an inbound Signal
message, a Telegram chat and an email thread resolve to one person. A write path
that inserts an unnormalized value does not raise -- it silently creates a second
person, and the contact graph quietly stops being a graph.

Normalization is lossy on purpose, so every caller stores BOTH: the normalized
form for matching, the raw form for display. Never show a user the normalized
value; never match on the raw one.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from server import config

# The channel kinds contact_channels understands. A new kind is one entry here
# plus one branch in normalize() -- nothing else in the codebase enumerates
# them.
CHANNEL_KINDS = ("email", "phone", "signal", "telegram", "handle")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NON_DIGIT = re.compile(r"\D")
_TELEGRAM_RE = re.compile(r"^[a-z0-9_]{5,32}$")


class NormalizationError(ValueError):
    """Raised when a value cannot be normalized for the given kind. A
    ValueError subclass deliberately: it is the same failure class that
    triggers the extraction repair-retry, so a bad LLM-supplied channel is
    repaired rather than written."""


# ── Email ────────────────────────────────────────────────────────────────────

def normalize_email(raw: Optional[str]) -> Optional[str]:
    """Lowercase, trimmed, angle-brackets and display name stripped.

        "Bob Smith <Bob@Acme.COM>"  -> "bob@acme.com"
        "  BOB@acme.com "           -> "bob@acme.com"
        "not-an-email"              -> None

    Deliberately does NOT strip Gmail-style dots or +tags. Two addresses that
    differ only by a plus-tag are frequently two real mailboxes, and collapsing
    them would merge two people permanently -- a destructive default. A CRM can
    offer that as an explicit merge; it must not guess it.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    # "Display Name <addr@host>" -- take the last angle-bracketed group.
    if "<" in value and ">" in value:
        start = value.rfind("<")
        end = value.find(">", start)
        if end > start:
            value = value[start + 1:end].strip()
    value = value.strip("\"'").strip()
    # Guard against the unicode homoglyph case before lowering.
    value = unicodedata.normalize("NFKC", value).lower()
    if not _EMAIL_RE.match(value):
        return None
    return value


def email_domain(raw: Optional[str]) -> Optional[str]:
    """Normalized domain, without the '@'. Used for organization matching and
    for trusted-sender domain patterns."""
    normalized = normalize_email(raw)
    if normalized is None:
        return None
    return normalized.partition("@")[2] or None


# ── Phone ────────────────────────────────────────────────────────────────────

def normalize_phone(raw: Optional[str], *, default_country_code: Optional[str] = None) -> Optional[str]:
    """E.164-ish: '+' followed by digits only.

        "+1 (555) 010-0123"  -> "+15550100123"
        "555-010-0123"       -> "+15550100123"   (default country code)
        "00 44 20 7946 0000" -> "+442079460000"  (00 is an intl prefix)
        "12345"              -> None             (too short to be a number)

    Stdlib-only, so this is not libphonenumber and does not validate that a
    number is assignable -- it canonicalizes format. A number that survives is
    consistently comparable, which is what uniqueness on contact_channels needs.

    A leading '+' or '00' is always honored as an explicit country code, so the
    default is applied only to genuinely national-format input.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None

    # Strip an extension before digit extraction, or "x123" becomes part of the
    # number itself.
    value = re.split(r"(?i)\b(?:ext|x|extension)\.?\s*\d+$", value)[0]

    explicit_intl = value.startswith("+")
    digits = _NON_DIGIT.sub("", value)
    if not digits:
        return None

    if not explicit_intl and digits.startswith("00"):
        digits = digits[2:]
        explicit_intl = True

    if not explicit_intl:
        cc = (default_country_code or config.get_default_country_code()).lstrip("+")
        # A national-format number that already carries the country code (a US
        # 11-digit starting with 1) must not have it prepended twice.
        if not (digits.startswith(cc) and len(digits) > 10):
            digits = cc + digits

    # Shortest plausible E.164 is 7 digits (country code + subscriber); longest
    # is 15 by spec. Anything outside is not a phone number.
    if not (7 <= len(digits) <= 15):
        return None
    return "+" + digits


# ── Messaging handles ────────────────────────────────────────────────────────

def normalize_telegram(raw: Optional[str]) -> Optional[str]:
    """Telegram @username, lowercased, '@' stripped, or a numeric chat id.

        "@Bob_Smith" -> "bob_smith"
        "123456789"  -> "123456789"   (numeric chat id, kept as-is)

    Telegram usernames are 5-32 chars of [A-Za-z0-9_] and case-insensitive.
    Numeric ids are passed through because a bot often only ever sees the id.
    """
    if raw is None:
        return None
    value = unicodedata.normalize("NFKC", str(raw).strip()).lstrip("@").lower()
    if not value:
        return None
    if value.isdigit():
        return value
    if not _TELEGRAM_RE.match(value):
        return None
    return value


def normalize_signal(raw: Optional[str]) -> Optional[str]:
    """Signal identities are phone numbers, so this is normalize_phone under a
    name that matches the channel kind. It exists so callers dispatch on kind
    without knowing that fact -- if Signal usernames become general, only this
    function changes."""
    return normalize_phone(raw)


def normalize_handle(raw: Optional[str]) -> Optional[str]:
    """A generic handle for channels with no stricter rule: trimmed, NFKC-
    normalized, lowercased, leading '@' removed."""
    if raw is None:
        return None
    value = unicodedata.normalize("NFKC", str(raw).strip()).lstrip("@").lower()
    return value or None


# ── Dispatch ─────────────────────────────────────────────────────────────────

_NORMALIZERS = {
    "email": normalize_email,
    "phone": normalize_phone,
    "signal": normalize_signal,
    "telegram": normalize_telegram,
    "handle": normalize_handle,
}


def normalize(kind: str, raw: Optional[str]) -> Optional[str]:
    """Normalize `raw` for channel `kind`. Returns None when the value cannot
    be normalized; raises NormalizationError for an unknown kind, so a typo in
    a kind fails loudly rather than silently storing an unmatched value."""
    normalizer = _NORMALIZERS.get(kind)
    if normalizer is None:
        raise NormalizationError(
            f"unknown channel kind {kind!r} (known: {', '.join(CHANNEL_KINDS)})"
        )
    return normalizer(raw)


def normalize_required(kind: str, raw: Optional[str]) -> str:
    """normalize(), but raises NormalizationError instead of returning None.
    Use at write paths, where a null normalized value must never reach
    contact_channels."""
    value = normalize(kind, raw)
    if value is None:
        raise NormalizationError(f"{raw!r} is not a valid {kind} value")
    return value


def same(kind: str, a: Optional[str], b: Optional[str]) -> bool:
    """True when two raw values normalize to the same thing. False if either
    fails to normalize -- two unparseable values are not 'equal'."""
    na, nb = normalize(kind, a), normalize(kind, b)
    return na is not None and na == nb


# ── Country ──────────────────────────────────────────────────────────────────

# ISO 3166-1 alpha-2, the full current set -- a domicile/tax-residence/
# citizenship field that silently rejects a real jurisdiction because it fell
# outside a hand-picked shortlist is worse than one that takes the whole list
# up front.
_ISO_3166_1_ALPHA_2 = frozenset({
    "AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT",
    "AU", "AW", "AX", "AZ",
    "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI", "BJ", "BL", "BM", "BN",
    "BO", "BQ", "BR", "BS", "BT", "BV", "BW", "BY", "BZ",
    "CA", "CC", "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN", "CO",
    "CR", "CU", "CV", "CW", "CX", "CY", "CZ",
    "DE", "DJ", "DK", "DM", "DO", "DZ",
    "EC", "EE", "EG", "EH", "ER", "ES", "ET",
    "FI", "FJ", "FK", "FM", "FO", "FR",
    "GA", "GB", "GD", "GE", "GF", "GG", "GH", "GI", "GL", "GM", "GN", "GP",
    "GQ", "GR", "GS", "GT", "GU", "GW", "GY",
    "HK", "HM", "HN", "HR", "HT", "HU",
    "ID", "IE", "IL", "IM", "IN", "IO", "IQ", "IR", "IS", "IT",
    "JE", "JM", "JO", "JP",
    "KE", "KG", "KH", "KI", "KM", "KN", "KP", "KR", "KW", "KY", "KZ",
    "LA", "LB", "LC", "LI", "LK", "LR", "LS", "LT", "LU", "LV", "LY",
    "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK", "ML", "MM", "MN", "MO",
    "MP", "MQ", "MR", "MS", "MT", "MU", "MV", "MW", "MX", "MY", "MZ",
    "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP", "NR", "NU", "NZ",
    "OM",
    "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM", "PN", "PR", "PS", "PT",
    "PW", "PY",
    "QA",
    "RE", "RO", "RS", "RU", "RW",
    "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM",
    "SN", "SO", "SR", "SS", "ST", "SV", "SX", "SY", "SZ",
    "TC", "TD", "TF", "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TO", "TR",
    "TT", "TV", "TW", "TZ",
    "UA", "UG", "UM", "US", "UY", "UZ",
    "VA", "VC", "VE", "VG", "VI", "VN", "VU",
    "WF", "WS",
    "YE", "YT",
    "ZA", "ZM", "ZW",
})


def normalize_country(raw: Optional[str]) -> Optional[str]:
    """ISO-3166-1 alpha-2, uppercased.

        "us"    -> "US"
        " gb  " -> "GB"
        "USA"   -> None   (alpha-3 not accepted -- one canonical form, not a
                            second one that has to be kept in sync)
        "Freedonia" -> None

    Used for `organizations.domicile_country` (formation jurisdiction),
    `persons.tax_residence_country` and `persons.citizenship_country` --
    three different real-world facts that happen to share one representation,
    and therefore one normalizer (R5).
    """
    if raw is None:
        return None
    value = str(raw).strip().upper()
    if not value:
        return None
    if value not in _ISO_3166_1_ALPHA_2:
        return None
    return value


# ── Ticker symbols ───────────────────────────────────────────────────────────

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}([.\-][A-Z0-9]{1,10})?$")


def normalize_ticker(raw: Optional[str]) -> Optional[str]:
    """Uppercased, trimmed exchange ticker.

        " brk.a "  -> "BRK.A"
        "aapl"     -> "AAPL"
        ""         -> None
        "not a ticker!" -> None

    One dot or hyphen is accepted (BRK.A/BRK-B class shares); a value needing
    more punctuation than that is not a ticker this normalizer accepts. Used
    for `modules/funds`' `security.ticker` -- the one place a listed
    portfolio company's exchange symbol is recorded (docs/VERTICAL-ASSET-
    MANAGEMENT.md's `is_public`/security split).
    """
    if raw is None:
        return None
    value = str(raw).strip().upper()
    if not value:
        return None
    if not _TICKER_RE.match(value):
        return None
    return value


# ── Display names ────────────────────────────────────────────────────────────

def normalize_name(raw: Optional[str]) -> Optional[str]:
    """Collapsed whitespace, NFKC. Used for the fallback dedupe key
    (normalized name + domain) -- never for matching on its own. Matching
    people by name alone merges strangers; docs/COMPETITIVE-ANALYSIS.md records
    why every serious sync implementation refuses to do it.
    """
    if raw is None:
        return None
    value = unicodedata.normalize("NFKC", str(raw)).strip()
    value = re.sub(r"\s+", " ", value)
    return value.casefold() or None
