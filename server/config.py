"""Single source of truth for every setting in the platform (R5).

Shape ported from CATO's cato_config.py: a config class, a module-level
singleton, and public getter functions. Adding a setting is one attribute in
__init__ plus one getter that reads _config -- never an os.environ.get() call
scattered somewhere else in the codebase.

Two kinds of settings, deliberately separated:

  * Infrastructure (database, bind address, session lifetime) lives here and
    comes from the environment. Changing it needs a restart.
  * Product settings (approval mode, LLM provider, sync cadence) live in
    Postgres, per org, and are hot-reloaded. They are NOT here -- see
    server/core/settings.py once M2 lands.

Secrets NEVER live in the database and never in a config file. They come from
the environment and are read through get_secret(), which exists so there is one
place that knows which env var backs which logical name.

The read-merge-write discipline that applies to product settings does not apply
to this file, because nothing writes it. That is the point of the split: a
sibling project destroyed 15 settings keys on every save by loading a subset and
writing it back as the whole file (see docs/SOURCE-PATTERNS.md).
"""
from __future__ import annotations

import os
from typing import Any, Optional


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class Config:
    """Infrastructure settings, read once from the environment at import."""

    def __init__(self) -> None:
        # ── Database ──────────────────────────────────────────────────────
        # One database. Logical schemas (core/sync/jobs/ai) live inside it, so
        # there is no cross-database join problem to work around.
        self.db_host: str = os.environ.get("CRM_DB_HOST", "localhost")
        self.db_port: int = _as_int(os.environ.get("CRM_DB_PORT"), 5432)
        self.db_name: str = os.environ.get("CRM_DB_NAME", "crm")
        self.db_user: str = os.environ.get("CRM_DB_USER", "crm_app")
        self.db_password: str = os.environ.get("CRM_DB_PASSWORD", "")

        # Pool bounds. The pool is a singleton (R5) -- see server/db/pool.py.
        self.db_pool_min: int = _as_int(os.environ.get("CRM_DB_POOL_MIN"), 1)
        self.db_pool_max: int = _as_int(os.environ.get("CRM_DB_POOL_MAX"), 10)
        self.db_connect_timeout: int = _as_int(os.environ.get("CRM_DB_CONNECT_TIMEOUT"), 10)

        # A transaction-mode pooler (pgbouncer) in front of Postgres changes
        # how tenant context must be set: SET LOCAL inside the transaction,
        # never a plain SET, which would leak to the next borrower of the
        # connection. We always use SET LOCAL, so this flag only disables
        # server-side prepared statements, which pgbouncer cannot support.
        self.db_transaction_pooler: bool = _as_bool(
            os.environ.get("CRM_DB_TRANSACTION_POOLER", "false")
        )

        # ── Server ────────────────────────────────────────────────────────
        self.host: str = os.environ.get("CRM_HOST", "127.0.0.1")
        self.port: int = _as_int(os.environ.get("CRM_PORT"), 8500)
        # Must match the redirect URI registered with each OAuth provider.
        self.public_base_url: str = os.environ.get(
            "CRM_PUBLIC_BASE_URL", "http://localhost:8500"
        ).rstrip("/")

        # ── Sessions ──────────────────────────────────────────────────────
        self.session_ttl_seconds: int = _as_int(
            os.environ.get("CRM_SESSION_TTL_SECONDS"), 60 * 60 * 24 * 14
        )
        self.session_idle_seconds: int = _as_int(
            os.environ.get("CRM_SESSION_IDLE_SECONDS"), 60 * 60 * 24 * 7
        )
        self.session_cookie_name: str = os.environ.get("CRM_SESSION_COOKIE", "crm_session")
        # Cookies are Secure by default; set false only for local plain-HTTP
        # development. Unlike a sibling project's auth gate, nothing here fails
        # open -- see first_run below.
        self.session_cookie_secure: bool = _as_bool(
            os.environ.get("CRM_SESSION_COOKIE_SECURE", "true")
        )

        # ── Identity normalization ────────────────────────────────────────
        # Default calling code for phone numbers written without one. Contact
        # channels are unique on the normalized value, so this decides whether
        # "555-0100" and "+1 555 0100" resolve to the same person.
        self.default_country_code: str = os.environ.get("CRM_DEFAULT_COUNTRY_CODE", "1").lstrip("+")

        # ── First run ─────────────────────────────────────────────────────
        # An unconfigured multi-user CRM fails CLOSED. The only route available
        # before an admin exists is the first-run setup path, and it stops
        # working the moment one does. (Cal fails open when unconfigured; that
        # choice exists so a single operator cannot lock themselves out of their
        # own Settings page, and it does not transfer here.)
        self.allow_first_run_setup: bool = _as_bool(
            os.environ.get("CRM_ALLOW_FIRST_RUN_SETUP", "true")
        )

        # ── Live-action gates ─────────────────────────────────────────────
        # Every destructive/outbound path is off unless explicitly enabled.
        # These are the master switches; per-org approval mode gates the rest.
        self.calendar_write_enabled: bool = _as_bool(
            os.environ.get("CRM_CALENDAR_WRITE_ENABLED", "false")
        )
        self.send_mail_enabled: bool = _as_bool(
            os.environ.get("CRM_SEND_MAIL_ENABLED", "false")
        )
        self.contact_writeback_enabled: bool = _as_bool(
            os.environ.get("CRM_CONTACT_WRITEBACK_ENABLED", "false")
        )
        self.messaging_send_enabled: bool = _as_bool(
            os.environ.get("CRM_MESSAGING_SEND_ENABLED", "false")
        )


# The singleton. Import the getters below, not this.
_config = Config()


def get_config() -> Config:
    return _config


def reload_config() -> Config:
    """Re-read the environment. Used by tests; not a hot-reload path for
    running processes, which restart instead."""
    global _config
    _config = Config()
    return _config


# ── Database ─────────────────────────────────────────────────────────────────

def get_db_dsn() -> str:
    """libpq connection string. Password is omitted when empty so local peer
    or trust authentication works without inventing a blank password."""
    parts = [
        f"host={_config.db_host}",
        f"port={_config.db_port}",
        f"dbname={_config.db_name}",
        f"user={_config.db_user}",
        f"connect_timeout={_config.db_connect_timeout}",
        "application_name=crm",
    ]
    if _config.db_password:
        parts.append(f"password={_config.db_password}")
    return " ".join(parts)


def get_db_name() -> str:
    return _config.db_name


def get_db_pool_bounds() -> tuple[int, int]:
    return _config.db_pool_min, _config.db_pool_max


def uses_transaction_pooler() -> bool:
    return _config.db_transaction_pooler


# ── Server ───────────────────────────────────────────────────────────────────

def get_host() -> str:
    return _config.host


def get_port() -> int:
    return _config.port


def get_public_base_url() -> str:
    return _config.public_base_url


# ── Sessions ─────────────────────────────────────────────────────────────────

def get_session_ttl_seconds() -> int:
    return _config.session_ttl_seconds


def get_session_idle_seconds() -> int:
    return _config.session_idle_seconds


def get_session_cookie_name() -> str:
    return _config.session_cookie_name


def session_cookie_secure() -> bool:
    return _config.session_cookie_secure


def allow_first_run_setup() -> bool:
    return _config.allow_first_run_setup


# ── Identity ─────────────────────────────────────────────────────────────────

def get_default_country_code() -> str:
    return _config.default_country_code


# ── Live-action gates ────────────────────────────────────────────────────────

def calendar_write_enabled() -> bool:
    return _config.calendar_write_enabled


def send_mail_enabled() -> bool:
    return _config.send_mail_enabled


def contact_writeback_enabled() -> bool:
    return _config.contact_writeback_enabled


def messaging_send_enabled() -> bool:
    return _config.messaging_send_enabled


# ── Secrets ──────────────────────────────────────────────────────────────────

# The one mapping from logical secret name to environment variable. Provider-
# prefixed throughout so a new provider slots in without renaming anything.
_SECRET_ENV = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    "claudecode_key": "CLAUDECODE_KEY",
    "microsoft_oauth_client_id": "MICROSOFT_OAUTH_CLIENT_ID",
    "microsoft_oauth_client_secret": "MICROSOFT_OAUTH_CLIENT_SECRET",
    "google_oauth_client_id": "GOOGLE_OAUTH_CLIENT_ID",
    "google_oauth_client_secret": "GOOGLE_OAUTH_CLIENT_SECRET",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "signal_cli_number": "SIGNAL_CLI_NUMBER",
}


def secret_names() -> list[str]:
    return sorted(_SECRET_ENV)


def get_secret(name: str) -> str:
    """Read a secret from the environment. Returns '' when unset -- callers
    decide whether that is fatal, because it depends on whether the provider is
    in use. Raises for an unknown name so a typo fails loudly instead of
    silently reading as unconfigured."""
    env_var = _SECRET_ENV.get(name)
    if env_var is None:
        raise KeyError(f"unknown secret {name!r} (known: {', '.join(secret_names())})")
    return os.environ.get(env_var, "")


def is_secret_set(name: str) -> bool:
    return bool(get_secret(name).strip())


def secret_env_var(name: str) -> Optional[str]:
    return _SECRET_ENV.get(name)
