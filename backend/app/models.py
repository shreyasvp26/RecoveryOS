"""RecoveryOS domain model — the locked PaymentEvent contract.

Phase 2 only: establishes the internal domain data contract and validation.
No business, policy, executor, or benchmark logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Locked finite value sets.
PAYMENT_METHODS: frozenset[str] = frozenset({"upi", "card", "netbanking", "wallet"})
RISK_FLAGS: frozenset[str] = frozenset({"normal", "fraud_suspect"})

CUSTOMER_HISTORY_KEYS: frozenset[str] = frozenset(
    {"prior_successful_payments", "prior_failed_payments", "has_active_subscription"}
)


@dataclass(frozen=True)
class CustomerHistory:
    """Structured customer history required by the locked PaymentEvent contract."""

    prior_successful_payments: int
    prior_failed_payments: int
    has_active_subscription: bool

    def __post_init__(self) -> None:
        for name in ("prior_successful_payments", "prior_failed_payments"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"customer_history.{name} must be an integer")
            if value < 0:
                raise ValueError(f"customer_history.{name} must be non-negative")
        if not isinstance(self.has_active_subscription, bool):
            raise ValueError("customer_history.has_active_subscription must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "prior_successful_payments": self.prior_successful_payments,
            "prior_failed_payments": self.prior_failed_payments,
            "has_active_subscription": self.has_active_subscription,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CustomerHistory":
        if not isinstance(data, dict):
            raise ValueError("customer_history must be an object")
        if any(key not in CUSTOMER_HISTORY_KEYS for key in data):
            raise ValueError("customer_history contains unexpected keys")
        if any(key not in data for key in CUSTOMER_HISTORY_KEYS):
            raise ValueError("customer_history is missing required fields")
        return cls(
            prior_successful_payments=data["prior_successful_payments"],
            prior_failed_payments=data["prior_failed_payments"],
            has_active_subscription=data["has_active_subscription"],
        )


@dataclass(frozen=True)
class PaymentEvent:
    """The locked PaymentEvent domain contract.

    WARNING: Do not add, remove, or rename fields. This contract is locked.
    Specifically, payment_id MUST remain part of the contract.
    """

    event_id: str
    order_id: str
    payment_id: str
    customer_id: str
    amount_paise: int
    currency: str
    payment_method: str
    failure_reason: str
    bank: str
    risk_flag: str
    customer_history: CustomerHistory
    timestamp: str

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "order_id",
            "payment_id",
            "customer_id",
            "currency",
            "payment_method",
            "failure_reason",
            "bank",
            "risk_flag",
            "timestamp",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

        if not isinstance(self.amount_paise, int) or isinstance(self.amount_paise, bool):
            raise ValueError("amount_paise must be an integer (paise)")
        if self.amount_paise < 0:
            raise ValueError("amount_paise must be non-negative")

        if self.payment_method not in PAYMENT_METHODS:
            raise ValueError(
                f"payment_method must be one of {sorted(PAYMENT_METHODS)}, got {self.payment_method!r}"
            )
        if self.risk_flag not in RISK_FLAGS:
            raise ValueError(
                f"risk_flag must be one of {sorted(RISK_FLAGS)}, got {self.risk_flag!r}"
            )

        if not isinstance(self.customer_history, CustomerHistory):
            raise ValueError("customer_history must be a CustomerHistory instance")

        # Timestamp must be a valid ISO8601 date-time (not merely a calendar date).
        parsed = datetime.fromisoformat(self.timestamp)
        if not (parsed.hour or parsed.minute or parsed.second or parsed.microsecond):
            raise ValueError(
                "timestamp must include a time component (ISO8601 date-time)"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict, preserving the locked contract exactly."""
        return {
            "event_id": self.event_id,
            "order_id": self.order_id,
            "payment_id": self.payment_id,
            "customer_id": self.customer_id,
            "amount_paise": self.amount_paise,
            "currency": self.currency,
            "payment_method": self.payment_method,
            "failure_reason": self.failure_reason,
            "bank": self.bank,
            "risk_flag": self.risk_flag,
            "customer_history": self.customer_history.to_dict(),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaymentEvent":
        """Reconstruct a PaymentEvent from a plain dict."""
        if not isinstance(data, dict):
            raise ValueError("PaymentEvent data must be an object")
        required = {
            "event_id",
            "order_id",
            "payment_id",
            "customer_id",
            "amount_paise",
            "currency",
            "payment_method",
            "failure_reason",
            "bank",
            "risk_flag",
            "customer_history",
            "timestamp",
        }
        if any(key not in data for key in required):
            raise ValueError("PaymentEvent data is missing required fields")
        if any(key not in required for key in data):
            raise ValueError("PaymentEvent data contains unexpected fields")
        return cls(
            event_id=data["event_id"],
            order_id=data["order_id"],
            payment_id=data["payment_id"],
            customer_id=data["customer_id"],
            amount_paise=data["amount_paise"],
            currency=data["currency"],
            payment_method=data["payment_method"],
            failure_reason=data["failure_reason"],
            bank=data["bank"],
            risk_flag=data["risk_flag"],
            customer_history=CustomerHistory.from_dict(data["customer_history"]),
            timestamp=data["timestamp"],
        )
