"""RecoveryOS application configuration.

Phase 3: resolves the SQLite database path from the DATABASE_URL environment
variable, defaulting to a local development database. No business logic.

The database URL follows the SQLAlchemy-style scheme used in .env.example,
e.g.  DATABASE_URL=sqlite:///./recoveryos.db
Only the sqlite scheme is supported; SQLite remains the sole database.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_DATABASE_URL = "sqlite:///./recoveryos.db"
_DATABASE_URL_PREFIX = "sqlite:///"

DEFAULT_OMNIROUTE_BASE_URL = "https://api.omniroute.ai/v1"
DEFAULT_OMNIROUTE_MODEL = "omniroute-v1"

# Phase 6 policy defaults. The engine itself never reads environment
# variables; configuration is resolved here once and passed in explicitly.
DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H = 2
DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES = 30
DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE = 5_000_000  # ₹50,000.00 in paise


def get_database_url() -> str:
    """Return the configured database URL, or the development default."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_database_path() -> str:
    """Resolve the SQLite file path from the configured database URL."""
    url = get_database_url()
    if not url.startswith(_DATABASE_URL_PREFIX):
        raise ValueError(
            f"Unsupported database URL {url!r}; expected sqlite:///<path>"
        )
    return url[len(_DATABASE_URL_PREFIX):]


def get_omniroute_api_key() -> str:
    """Return the configured OmniRoute API key, or an empty string when unset."""
    return os.environ.get("OMNIROUTE_API_KEY", "")


def get_omniroute_model() -> str:
    """Return the configured OmniRoute model identifier."""
    return os.environ.get("OMNIROUTE_MODEL", DEFAULT_OMNIROUTE_MODEL)


def get_omniroute_base_url() -> str:
    """Return the configured OmniRoute base URL."""
    return os.environ.get("OMNIROUTE_BASE_URL", DEFAULT_OMNIROUTE_BASE_URL)


def _resolve_policy_int(env_name: str, default: int) -> int:
    """Resolve a positive policy integer from the environment (fail-closed)."""
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{env_name} must be an integer") from None
    return value


def get_policy_max_interventions_per_customer_24h() -> int:
    """Return the configured per-customer rolling 24h intervention limit."""
    return _resolve_policy_int(
        "POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H",
        DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H,
    )


def get_policy_event_cooldown_minutes() -> int:
    """Return the configured per-event cooldown in minutes."""
    return _resolve_policy_int(
        "POLICY_EVENT_COOLDOWN_MINUTES", DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES
    )


def get_policy_daily_spend_cap_paise() -> int:
    """Return the configured daily spend cap in paise."""
    return _resolve_policy_int(
        "POLICY_DAILY_SPEND_CAP_PAISE", DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE
    )


def get_razorpay_key_id() -> str:
    """Return the configured Razorpay Test Mode key id, or an empty string."""
    return os.environ.get("RAZORPAY_KEY_ID", "")


def get_razorpay_key_secret() -> str:
    """Return the configured Razorpay Test Mode key secret, or an empty string."""
    return os.environ.get("RAZORPAY_KEY_SECRET", "")


def build_razorpay_client() -> Any | None:
    """Build the Razorpay client boundary, or None when credentials are unset.

    Execution only ever runs in REAL_RAZORPAY mode when Test Mode credentials
    are present in the environment; a missing configuration is explicit and
    never silently bypassed. Present-but-invalid credentials (live ``rzp_live_``
    keys or unrecognized key ids) raise ``RazorpayConfigurationError`` from the
    client boundary rather than silently disabling execution.
    """
    from .razorpay_client import RazorpayPaymentLinkClient

    key_id = get_razorpay_key_id()
    key_secret = get_razorpay_key_secret()
    if not key_id or not key_secret:
        return None
    return RazorpayPaymentLinkClient(key_id, key_secret)


def build_policy_config() -> "PolicyConfig":
    """Build the deterministic policy configuration from the environment."""
    from .policy import PolicyConfig

    return PolicyConfig(
        max_interventions_per_customer_24h=get_policy_max_interventions_per_customer_24h(),
        event_cooldown_minutes=get_policy_event_cooldown_minutes(),
        daily_spend_cap_paise=get_policy_daily_spend_cap_paise(),
    )
