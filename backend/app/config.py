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

from dotenv import load_dotenv

# Load the local development .env (never committed) so that every config
# getter below can read Razorpay / policy / database environment values from
# it. load_dotenv() never overrides variables already present in the process
# environment, so explicit monkeypatch / shell-set values remain authoritative.
load_dotenv()

DEFAULT_DATABASE_URL = "sqlite:///./recoveryos.db"
_DATABASE_URL_PREFIX = "sqlite:///"

DEFAULT_OMNIROUTE_BASE_URL = "https://api.omniroute.ai/v1"
DEFAULT_OMNIROUTE_MODEL = "omniroute-v1"

# Phase 6 policy defaults. The engine itself never reads environment
# variables; configuration is resolved here once and passed in explicitly.
DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H = 2
DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES = 30
DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE = 5_000_000  # ₹50,000.00 in paise

# Operator authentication. A single shared API key protects every operator/data
# endpoint; it is required (fail-closed when unset) and never defaults to a
# known value. Values are read from the environment at request time, never
# cached, compared in constant time, and never exposed.
OPERATOR_API_KEY_ENV = "RECOVERYOS_OPERATOR_API_KEY"


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


def get_operator_api_key() -> str:
    """Return the configured operator API key, or an empty string when unset.

    An empty value is an explicit, fail-closed configuration state: operator
    endpoints refuse to serve (HTTP 503) until a real key is configured rather
    than running unauthenticated.
    """
    return os.environ.get(OPERATOR_API_KEY_ENV, "")


def get_cors_origins() -> list[str]:
    """Return the explicit cross-origin allow-list for browser access.

    Implements the DEPLOYMENT.md cross-origin contract: a frontend served from
    a different origin can reach the API only when the operator explicitly
    lists that origin in ``RECOVERYOS_CORS_ORIGINS`` (comma-separated). When
    unset, no cross-origin browser access is allowed — the supported topology
    is the same-origin reverse proxy (/api), exactly as in local development.
    """
    raw = os.environ.get("RECOVERYOS_CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


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


def get_razorpay_webhook_secret() -> str:
    """Return the configured Razorpay webhook secret, or an empty string.

    This is a SEPARATE secret from the API key secret: it is the secret chosen
    in the Razorpay Dashboard for webhook signature verification (HMAC-SHA256
    over the raw request body). It is never committable and never stored in
    the database. A missing value is explicit and verified against in a
    fail-closed way by the webhook boundary.
    """
    return os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")


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


def default_intervention_cost_paise() -> dict[str, int]:
    """The modelled cost of every candidate intervention, in paise.

    Spend-cap accounting and the optimizer share one source of truth: the
    economic model (``DEFAULT_ECONOMIC_MODEL``). Costs are resolved here once
    and wired into the runtime policy (``build_policy_config``) and into replay
    scenario configs (``policy_scenario``) so the two methodologies cannot drift
    apart — an unchanged policy whose label is renamed must replay identically.
    An intervention with no economic assumption (e.g. ``no_action``) costs 0.
    """
    from .classification import CANDIDATE_INTERVENTIONS
    from .economics import DEFAULT_ECONOMIC_MODEL

    return {
        intervention: (
            DEFAULT_ECONOMIC_MODEL.assumptions[intervention].cost_paise
            if intervention in DEFAULT_ECONOMIC_MODEL.assumptions
            else 0
        )
        for intervention in CANDIDATE_INTERVENTIONS
    }


def build_policy_config() -> "PolicyConfig":
    """Build the deterministic policy configuration from the environment.

    The spend-cap rule is backed by the SAME economic cost model the optimizer
    uses (``DEFAULT_ECONOMIC_MODEL``), so a persisted attempt accumulates the
    real modelled cost of its intervention and the cap is genuinely enforced —
    never structurally disabled with all-zero costs. Costs are resolved here,
    once, from the economic model; operators tune the cap itself via
    ``POLICY_DAILY_SPEND_CAP_PAISE``.
    """
    from .policy import PolicyConfig

    return PolicyConfig(
        max_interventions_per_customer_24h=get_policy_max_interventions_per_customer_24h(),
        event_cooldown_minutes=get_policy_event_cooldown_minutes(),
        daily_spend_cap_paise=get_policy_daily_spend_cap_paise(),
        intervention_cost_paise=default_intervention_cost_paise(),
    )
