"""Deterministic recovery outcome simulation — the evaluation boundary (Phase 8).

The evaluation boundary is the ONLY place the System Under Test meets hidden
ground truth. It receives: the original event, the hidden outcome model, and
the intervention that the selector already chose. It NEVER classifies,
authorizes, selects, executes, or calls the LLM/Razorpay.

Recovery is simulated as a single Bernoulli draw per (event, intervention)
using a private ``random.Random(f"{seed}:{event_id}:{intervention}")``
instance. The draw for a given triple therefore does not depend on evaluation
order, on how other events were simulated, or on prior simulations of the same
event — only on (master seed, event identity, intervention).

Execution continuity: recovering money is different from an intervention
executing. ``ExecutionOutcome.status == "SUCCESS"`` only means the operation
ran; recovery here is decided independently and may be False even for a fully
successful execution. ``no_action`` is never executed by the System Under
Test; the outcome layer only models its natural baseline.
"""

from __future__ import annotations

import random
from typing import Any

from .classification import CANDIDATE_INTERVENTIONS
from .models import PaymentEvent
from .outcome_model import HiddenOutcomeModel, InvalidSeedError, OutcomeModelError


class RecoveryOutcome:
    """The simulated result of one intervention on one event.

    Minimal by design: the record carries ``event_id``, the intervention, a
    boolean ``recovered``, and the derived recovered amount. Hidden recovery
    probabilities never appear in this record.
    """

    def __init__(
        self,
        event_id: str,
        intervention: str,
        recovered: bool,
        recovered_amount_paise: int,
    ) -> None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise OutcomeModelError("event_id must be a non-empty string")
        _validate_intervention(intervention)
        if type(recovered) is not bool:
            raise OutcomeModelError(
                f"recovered must be a boolean, got {recovered!r}"
            )
        if (
            type(recovered_amount_paise) is not int
            or recovered_amount_paise < 0
        ):
            raise OutcomeModelError(
                "recovered_amount_paise must be a non-negative integer"
            )
        self.event_id = event_id
        self.intervention = intervention
        self.recovered = recovered
        self.recovered_amount_paise = recovered_amount_paise

    def to_dict(self) -> dict[str, object]:
        """Dictionary view for evaluation harnesses and tests."""
        return {
            "event_id": self.event_id,
            "intervention": self.intervention,
            "recovered": self.recovered,
            "recovered_amount_paise": self.recovered_amount_paise,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RecoveryOutcome):
            return NotImplemented
        return self.to_dict() == other.to_dict()


def _validate_intervention(intervention: Any) -> str:
    """Reject interventions outside the locked taxonomy (fail closed)."""
    if intervention not in CANDIDATE_INTERVENTIONS:
        raise OutcomeModelError(
            f"intervention {intervention!r} is not a locked intervention; "
            f"expected one of {sorted(CANDIDATE_INTERVENTIONS)}"
        )
    return intervention


def _validate_seed(seed: Any) -> int:
    if type(seed) is not int:
        raise InvalidSeedError(
            f"outcome simulation seed must be an integer, got {seed!r}"
        )
    return seed


class OutcomeSimulator:
    """Simulates intervention outcomes against the hidden outcome model.

    The simulator is evaluation-only. It validates the event and intervention,
    retrieves the hidden ground-truth probability from the model, and draws a
    single deterministic Bernoulli outcome. Simulations are reproducible for
    (model seed, event identity, intervention) regardless of ordering.
    """

    def __init__(self, model: HiddenOutcomeModel) -> None:
        if not isinstance(model, HiddenOutcomeModel):
            raise OutcomeModelError(
                f"expected a HiddenOutcomeModel, got {type(model).__name__}"
            )
        self._model = model

    @property
    def model(self) -> HiddenOutcomeModel:
        """The hidden outcome model this simulator evaluates against."""
        return self._model

    @property
    def seed(self) -> int:
        """The master seed carried by the hidden model."""
        return self._model.seed

    def simulate(
        self,
        event: PaymentEvent,
        intervention: str,
    ) -> RecoveryOutcome:
        """Simulate the recovery outcome of ``intervention`` on ``event``.

        A missing event identity or intervention in the model raises an
        explicit ``MissingGroundTruthError``; a malformed event or intervention
        fails closed with ``OutcomeModelError``. Nothing is ever guessed.
        """
        if not isinstance(event, PaymentEvent):
            raise OutcomeModelError(
                f"expected PaymentEvent, got {type(event).__name__}"
            )
        _validate_intervention(intervention)

        seed = self._model.seed
        probability = self._model.recovery_probability(
            event.event_id, intervention
        )
        draw = random.Random(
            f"{seed}:{event.event_id}:{intervention}"
        ).random()
        recovered = draw < probability
        return RecoveryOutcome(
            event_id=event.event_id,
            intervention=intervention,
            recovered=recovered,
            recovered_amount_paise=event.amount_paise if recovered else 0,
        )
