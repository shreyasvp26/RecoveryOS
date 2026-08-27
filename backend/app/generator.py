"""Deterministic synthetic PaymentEvent generation.

Phase 4: produces realistic-looking development data from a seeded random
source. The same seed and generation parameters always reproduce the exact
same dataset. Generated events contain decision-time information only; the
generator has no knowledge of recovery outcomes, benchmark scores, or future
ground truth. This is NOT the benchmark generator and contains no recovery
model.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .models import CustomerHistory, PaymentEvent

DEFAULT_SEED = 42
DEFAULT_COUNT = 10

_CURRENCY = "INR"

# Locked domain values are listed as ordered tuples so that random.choice is
# stable regardless of frozenset iteration order / hash randomization.
_PAYMENT_METHODS: tuple[str, ...] = ("upi", "card", "netbanking", "wallet")
_RISK_FLAGS: tuple[str, ...] = ("normal", "fraud_suspect")

_BANKS: tuple[str, ...] = (
    "HDFC",
    "ICICI",
    "SBI",
    "Axis",
    "Kotak",
    "Yes Bank",
    "Paytm Payments Bank",
)

# The Phase 2 contract requires failure_reason to be a non-empty string and
# defines no finite taxonomy, so the generator draws from a fixed set of
# realistic values that satisfy the locked contract.
_FAILURE_REASONS: tuple[str, ...] = (
    "bank_timeout",
    "insufficient_funds",
    "authentication_failed",
    "declined_by_bank",
    "expired_card",
    "transaction_declined",
    "payment_failed",
    "network_issue",
)

# Fixed reference window so timestamps are deterministic and never use
# nondeterministic wall-clock time.
_WINDOW_START = datetime(2026, 7, 1, tzinfo=timezone.utc)
_WINDOW_END = datetime(2026, 8, 27, tzinfo=timezone.utc)
_WINDOW_SECONDS = int((_WINDOW_END - _WINDOW_START).total_seconds())

_MIN_AMOUNT_PAISE = 500  # ₹5
_MAX_AMOUNT_PAISE = 20000  # ₹20,000


def _generate_timestamp(rng: random.Random) -> str:
    """Draw a valid ISO8601 date-time within the fixed reference window."""
    timestamp = _WINDOW_START + timedelta(seconds=rng.randrange(_WINDOW_SECONDS))
    if not (timestamp.hour or timestamp.minute or timestamp.second or timestamp.microsecond):
        timestamp = timestamp.replace(second=1)
    return timestamp.isoformat()


def _generate_amount_paise(rng: random.Random) -> int:
    """Draw a realistic whole-rupee amount in paise (integer only)."""
    return rng.randint(_MIN_AMOUNT_PAISE, _MAX_AMOUNT_PAISE) * 100


def _generate_customer_history(rng: random.Random) -> CustomerHistory:
    """Draw a CustomerHistory that satisfies the locked contract validation."""
    return CustomerHistory(
        prior_successful_payments=rng.randint(0, 40),
        prior_failed_payments=rng.randint(0, 6),
        has_active_subscription=rng.random() < 0.35,
    )


def _customer_pool(
    rng: random.Random, count: int
) -> list[tuple[str, CustomerHistory]]:
    """Build a deterministic pool of synthetic customers for the dataset."""
    pool_size = max(1, (count + 2) // 3)
    return [
        (f"cust_{index + 1:04d}", _generate_customer_history(rng))
        for index in range(pool_size)
    ]


def generate_events(
    seed: int = DEFAULT_SEED, count: int = DEFAULT_COUNT
) -> list[PaymentEvent]:
    """Return a deterministic list of `count` synthetic PaymentEvents.

    Event, order, and payment identifiers are unique within the returned
    dataset. Customer identifiers may repeat because multiple events may
    belong to the same synthetic customer.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    rng = random.Random(seed)
    customers = _customer_pool(rng, count)
    events: list[PaymentEvent] = []
    for index in range(count):
        customer_id, history = customers[rng.randrange(len(customers))]
        events.append(
            PaymentEvent(
                event_id=f"evt_{index + 1:06d}",
                order_id=f"order_{index + 1:06d}",
                payment_id=f"pay_{index + 1:06d}",
                customer_id=customer_id,
                amount_paise=_generate_amount_paise(rng),
                currency=_CURRENCY,
                payment_method=rng.choice(_PAYMENT_METHODS),
                failure_reason=rng.choice(_FAILURE_REASONS),
                bank=rng.choice(_BANKS),
                risk_flag=rng.choice(_RISK_FLAGS),
                customer_history=history,
                timestamp=_generate_timestamp(rng),
            )
        )
    return events


def generate_event_dicts(
    seed: int = DEFAULT_SEED, count: int = DEFAULT_COUNT
) -> list[dict]:
    """Return the generated dataset serialized to the locked dict contract."""
    return [event.to_dict() for event in generate_events(seed, count)]


def _main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic synthetic PaymentEvent dataset."
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    args = parser.parse_args(argv)
    print(json.dumps(generate_event_dicts(args.seed, args.count), indent=2))


if __name__ == "__main__":
    _main()
