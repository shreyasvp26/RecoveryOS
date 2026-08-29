"""Simulated execution boundary for batch benchmarking (Phase 17).

WHY THIS EXISTS SEPARATELY FROM ``executor.py``
-----------------------------------------------
The production ``BoundedExecutor`` couples ``payment_link`` to REAL_RAZORPAY by
construction, and rightly so: in production a payment link that was not
actually created must never be reported as created. The consequence is that a
batch benchmark run without credentials records every ``payment_link`` as
``configuration_missing``/FAILED, which silently removes an entire intervention
from the comparison and pins the benchmark to V1.

Phase 17 fixes that with a benchmark-owned simulator instead of by teaching the
production executor to pretend. The production Razorpay path is untouched: this
module has no Razorpay import, no network import, and no credential lookup, so
a batch run cannot reach a provider even by accident.

EXECUTION IS NOT RECOVERY
-------------------------
A simulated execution only means "the action was performed". Whether money came
back is decided afterwards and independently by the hidden world. A SUCCESS
here never implies recovery.

AUTHORIZATION
-------------
The simulator enforces the same authorization boundary as the production
executor: performing an action requires an authoritative ALLOW ``PolicyDecision``
bound to that exact event and intervention. The single exception is explicit and
must be requested by name — the Naive Retry baseline has no policy gate at all,
which is precisely what makes it naive, so it executes with
``require_authorization=False`` and its result is permanently stamped
``authorized=False`` for the safety metrics to count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .classification import CANDIDATE_INTERVENTIONS
from .economics import EXECUTABLE_INTERVENTIONS
from .models import PaymentEvent
from .policy import PolicyDecision
from .selector import NO_ACTION

SIMULATED = "SIMULATED"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"


class SimulatedExecutionError(Exception):
    """The simulator was asked to perform something it must refuse."""


class SimulatedAuthorizationError(SimulatedExecutionError):
    """A simulated execution was attempted without an authoritative ALLOW."""


@dataclass(frozen=True)
class SimulatedExecution:
    """The benchmark-only result of performing one intervention.

    Distinct from ``executor.ExecutionOutcome`` on purpose: this type can never
    be persisted through the production execution tables and can never be
    mistaken for a real provider result, because ``execution_mode`` is
    structurally pinned to SIMULATED for every intervention including
    ``payment_link``.
    """

    event_id: str
    intervention: str
    execution_mode: str
    status: str
    authorized: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise SimulatedExecutionError("event_id must be a non-empty string")
        if self.intervention not in EXECUTABLE_INTERVENTIONS:
            raise SimulatedExecutionError(
                f"intervention must be one of {sorted(EXECUTABLE_INTERVENTIONS)}, "
                f"got {self.intervention!r}"
            )
        if self.execution_mode != SIMULATED:
            raise SimulatedExecutionError(
                "a benchmark execution is always SIMULATED; a batch run must "
                "never report provider execution"
            )
        if self.status not in (STATUS_SUCCESS, STATUS_FAILED):
            raise SimulatedExecutionError(
                f"status must be {STATUS_SUCCESS!r} or {STATUS_FAILED!r}, "
                f"got {self.status!r}"
            )
        if type(self.authorized) is not bool:
            raise SimulatedExecutionError("authorized must be a boolean")
        if self.detail is not None and (
            not isinstance(self.detail, str) or not self.detail.strip()
        ):
            raise SimulatedExecutionError("detail must be None or a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the simulated execution for benchmark artifacts."""
        return {
            "event_id": self.event_id,
            "intervention": self.intervention,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "authorized": self.authorized,
            "detail": self.detail,
        }


class SimulatedExecutor:
    """Performs one intervention in simulation. Pure, offline, deterministic.

    Every executable intervention is representable, including ``payment_link``,
    with no credential and no network access. The simulator makes no recovery
    judgement whatsoever.
    """

    def execute(
        self,
        event: PaymentEvent,
        intervention: str,
        decision: PolicyDecision | None = None,
        *,
        require_authorization: bool = True,
    ) -> SimulatedExecution:
        """Simulate performing ``intervention`` on ``event``."""
        if not isinstance(event, PaymentEvent):
            raise SimulatedExecutionError("event must be a PaymentEvent")
        if intervention not in CANDIDATE_INTERVENTIONS:
            raise SimulatedExecutionError(
                f"intervention must be one of {sorted(CANDIDATE_INTERVENTIONS)}, "
                f"got {intervention!r}"
            )
        if intervention == NO_ACTION:
            raise SimulatedExecutionError(
                f"{NO_ACTION!r} is the absence of an action and is never executed"
            )

        authorized = self._authorization_state(
            event, intervention, decision, require_authorization
        )
        return SimulatedExecution(
            event_id=event.event_id,
            intervention=intervention,
            execution_mode=SIMULATED,
            status=STATUS_SUCCESS,
            authorized=authorized,
            detail=None,
        )

    @staticmethod
    def _authorization_state(
        event: PaymentEvent,
        intervention: str,
        decision: PolicyDecision | None,
        require_authorization: bool,
    ) -> bool:
        """Return whether this execution carries authoritative authorization.

        With ``require_authorization`` set, anything short of a genuine ALLOW
        bound to this event and this intervention stops the execution. Without
        it, no decision is consulted and the result is honestly recorded as
        unauthorized rather than being quietly upgraded.
        """
        if not require_authorization:
            return False
        if not isinstance(decision, PolicyDecision):
            raise SimulatedAuthorizationError(
                "simulated execution requires an authoritative PolicyDecision"
            )
        if decision.allowed is not True:
            raise SimulatedAuthorizationError(
                f"simulated execution denied: policy has not authorized "
                f"{intervention!r} (denial_reason={decision.denial_reason!r})"
            )
        if decision.event_id != event.event_id:
            raise SimulatedAuthorizationError(
                f"policy decision is for event {decision.event_id!r}, not "
                f"{event.event_id!r}"
            )
        if decision.proposed_intervention != intervention:
            raise SimulatedAuthorizationError(
                f"policy decision authorizes {decision.proposed_intervention!r}, "
                f"not {intervention!r}"
            )
        return True
