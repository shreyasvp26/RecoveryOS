"""Bounded intervention executor — outputs and authorization boundary.

Phase 7: the executor performs an authorized intervention through the correct
execution mode and produces an explicit ExecutionOutcome. It never calls the
LLM, never chooses interventions, never evaluates policy, never benchmarks
recovery, and never computes recovery probability. Execution requires an
authoritative PolicyDecision with allowed == True; anything else is rejected
before any action occurs (defense in depth after the deterministic gate).

Execution success is NOT revenue recovery success: an operation that ran
correctly reports SUCCESS regardless of whether any money was ultimately
recovered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .classification import CANDIDATE_INTERVENTIONS
from .models import PaymentEvent
from .policy import (
    PolicyDecision,
    PolicyValidationError,
    parse_aware_datetime,
)
from .razorpay_client import (
    RazorpayError,
    reference_id_from,
)

# Explicit, structured execution modes. payment_link is the only intervention
# that executes through REAL_RAZORPAY; everything else is SIMULATED.
EXECUTION_MODES: frozenset[str] = frozenset({"SIMULATED", "REAL_RAZORPAY"})
EXECUTION_STATUSES: frozenset[str] = frozenset({"SUCCESS", "FAILED"})

SIMULATED_INTERVENTIONS: frozenset[str] = frozenset(
    {"retry_immediate", "retry_delayed", "reminder", "alternate_method_prompt"}
)
PAYMENT_LINK: str = "payment_link"
NO_ACTION: str = "no_action"


class ExecutionError(Exception):
    """Base class for all explicit execution failures."""


class ExecutionAuthorizationError(ExecutionError):
    """Execution was attempted without authoritative policy authorization.

    This is the safety boundary inside the executor itself: a denied or
    mismatched policy decision never results in an execution.
    """


class ExecutionRejectedError(ExecutionError):
    """The requested intervention cannot be executed through this path.

    no_action is never executable; an intervention outside the locked
    taxonomy is rejected; an unconfigured execution path is rejected.
    """


@dataclass(frozen=True)
class ExecutionOutcome:
    """The explicit result of executing one intervention.

    Distinguishes at minimum: event_id, intervention, execution_mode
    (SIMULATED or REAL_RAZORPAY), and status (SUCCESS or FAILED). External
    reference information (e.g. a genuine Razorpay Payment Link short URL) and
    explicit failure detail are carried when justified. Execution success is
    kept strictly separate from revenue recovery success.
    """

    event_id: str
    intervention: str
    execution_mode: str
    status: str
    external_reference: str | None = None
    detail: str | None = None
    reported_at: str = ""
    payment_link_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ExecutionRejectedError("event_id must be a non-empty string")
        if self.intervention not in CANDIDATE_INTERVENTIONS:
            raise ExecutionRejectedError(
                f"intervention must be one of {sorted(CANDIDATE_INTERVENTIONS)}, "
                f"got {self.intervention!r}"
            )
        if self.intervention == NO_ACTION:
            raise ExecutionRejectedError("no_action is never executable")
        if self.execution_mode not in EXECUTION_MODES:
            raise ExecutionRejectedError(
                f"execution_mode must be one of {sorted(EXECUTION_MODES)}, "
                f"got {self.execution_mode!r}"
            )
        if self.status not in EXECUTION_STATUSES:
            raise ExecutionRejectedError(
                f"status must be one of {sorted(EXECUTION_STATUSES)}, "
                f"got {self.status!r}"
            )
        # The mode/intervention coupling is structural and cannot be confused:
        # only payment_link ever runs through REAL_RAZORPAY, and simulated
        # interventions never touch a payment provider.
        if self.intervention == PAYMENT_LINK:
            if self.execution_mode != "REAL_RAZORPAY":
                raise ExecutionRejectedError(
                    "payment_link must be executed through REAL_RAZORPAY"
                )
        elif self.execution_mode != "SIMULATED":
            raise ExecutionRejectedError(
                f"{self.intervention!r} must be executed through SIMULATED"
            )
        for name in ("external_reference", "detail", "payment_link_id"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ExecutionRejectedError(f"{name} must be None or a non-empty string")
        try:
            parse_aware_datetime(self.reported_at)
        except PolicyValidationError as exc:
            raise ExecutionRejectedError(f"reported_at is invalid: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict, preserving the outcome contract."""
        return {
            "event_id": self.event_id,
            "intervention": self.intervention,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "external_reference": self.external_reference,
            "detail": self.detail,
            "reported_at": self.reported_at,
            "payment_link_id": self.payment_link_id,
        }


class BoundedExecutor:
    """Executes exactly one authorized intervention and returns its outcome.

    Pure: the outcome is a function of (event, intervention, decision) plus an
    injected payment-provider boundary. It does not persist anything and does
    not call the LLM.
    """

    def execute(
        self,
        event: PaymentEvent,
        intervention: str,
        decision: PolicyDecision,
        razorpay_client: Any = None,
    ) -> ExecutionOutcome:
        """Execute the requested intervention under authoritative authorization."""
        if not isinstance(event, PaymentEvent):
            raise ExecutionRejectedError("event must be a PaymentEvent")
        if not isinstance(intervention, str) or intervention not in CANDIDATE_INTERVENTIONS:
            raise ExecutionRejectedError(
                f"intervention must be one of {sorted(CANDIDATE_INTERVENTIONS)}, "
                f"got {intervention!r}"
            )
        if intervention == NO_ACTION:
            raise ExecutionRejectedError("no_action is never executed")

        self._require_authorization(event, intervention, decision)

        if intervention in SIMULATED_INTERVENTIONS:
            return ExecutionOutcome(
                event_id=event.event_id,
                intervention=intervention,
                execution_mode="SIMULATED",
                status="SUCCESS",
                reported_at=decision.evaluated_at,
            )

        if intervention == PAYMENT_LINK:
            return self._execute_payment_link(event, decision, razorpay_client)

        raise ExecutionRejectedError(
            f"intervention {intervention!r} has no execution path"
        )

    @staticmethod
    def _execute_payment_link(
        event: PaymentEvent,
        decision: PolicyDecision,
        razorpay_client: Any,
    ) -> ExecutionOutcome:
        """Create a Razorpay Test Mode Payment Link for the selected event.

        Provider-side failures (configuration missing, API error, unexpected
        response) produce an explicit FAILED result with the provider error
        detail; they never become a fabricated success and the URL is never
        invented client-side.
        """
        if razorpay_client is None:
            return ExecutionOutcome(
                event_id=event.event_id,
                intervention=PAYMENT_LINK,
                execution_mode="REAL_RAZORPAY",
                status="FAILED",
                detail="configuration_missing: razorpay client is not configured",
                reported_at=decision.evaluated_at,
            )

        try:
            reference_id = reference_id_from(event.event_id)
        except ValueError as exc:
            return ExecutionOutcome(
                event_id=event.event_id,
                intervention=PAYMENT_LINK,
                execution_mode="REAL_RAZORPAY",
                status="FAILED",
                detail=f"invalid_reference: {exc}",
                reported_at=decision.evaluated_at,
            )

        try:
            result = razorpay_client.create_payment_link(
                amount_paise=event.amount_paise,
                currency=event.currency,
                reference_id=reference_id,
                description=f"RecoveryOS payment link for order {event.order_id}",
            )
        except RazorpayError as exc:
            return ExecutionOutcome(
                event_id=event.event_id,
                intervention=PAYMENT_LINK,
                execution_mode="REAL_RAZORPAY",
                status="FAILED",
                detail=str(exc),
                reported_at=decision.evaluated_at,
            )
        except Exception as exc:
            # Defense in depth: even an unexpected provider-boundary failure is
            # recorded as an explicit FAILED outcome, never a fabricated
            # success and never a silent pass. The exposed detail is a stable
            # controlled identifier — arbitrary provider exception text is not
            # surfaced as user/audit-facing detail.
            return ExecutionOutcome(
                event_id=event.event_id,
                intervention=PAYMENT_LINK,
                execution_mode="REAL_RAZORPAY",
                status="FAILED",
                detail="razorpay_api_error",
                reported_at=decision.evaluated_at,
            )

        return ExecutionOutcome(
            event_id=event.event_id,
            intervention=PAYMENT_LINK,
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference=result.short_url,
            reported_at=decision.evaluated_at,
            payment_link_id=result.id,
        )

    @staticmethod
    def _require_authorization(
        event: PaymentEvent, intervention: str, decision: PolicyDecision
    ) -> None:
        """Reject any execution that lacks an authoritative ALLOW decision."""
        if not isinstance(decision, PolicyDecision):
            raise ExecutionAuthorizationError(
                "execution requires an authoritative PolicyDecision"
            )
        if decision.allowed is not True:
            raise ExecutionAuthorizationError(
                "execution denied: policy has not authorized "
                f"{intervention!r} (denial_reason={decision.denial_reason!r})"
            )
        if decision.event_id != event.event_id:
            raise ExecutionAuthorizationError(
                f"policy decision is for event {decision.event_id!r}, not "
                f"{event.event_id!r}"
            )
        if decision.proposed_intervention != intervention:
            raise ExecutionAuthorizationError(
                f"policy decision authorizes {decision.proposed_intervention!r}, "
                f"not {intervention!r}"
            )
