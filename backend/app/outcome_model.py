"""Hidden recovery outcome model — evaluation-only ground truth (Phase 8).

MODEL OF THE WORLD (NOT FOR THE SYSTEM UNDER TEST): each synthetic event gets
its own intervention-specific recovery probability for exactly the locked
interventions, including ``no_action`` (the natural baseline). The model is
generated deterministically from an explicit seed and is owned by the
evaluation layer only: classifier, policy, selector, executor, and the Razorpay
boundary never receive or refer to it. It never becomes part of any decision
the system under test makes.

Determinism rule: probabilities are drawn from a per-event ``random.Random``
instance seeded with ``f"{seed}:{event_id}"``. The draws therefore depend only
on (seed, event_id) and NOT on event-set size, evaluation order, or any shared
mutable RNG state. The same seed and event set always produce the identical
model; a different seed normally produces a different one. No module-global
random state and no ``random.seed(...)`` are ever used.
"""

from __future__ import annotations

import random
from typing import Any, Iterable, Mapping

from .classification import CANDIDATE_INTERVENTIONS
from .models import PaymentEvent

# A deterministic draw order for the intervention probabilities. The taxonomy
# is a frozenset; drawing in sorted order keeps the derived probabilities
# reproducible across interpreter runs and versions.
_DRAW_ORDER: tuple[str, ...] = tuple(sorted(CANDIDATE_INTERVENTIONS))


class OutcomeModelError(Exception):
    """Base class for all explicit hidden outcome model failures."""


class InvalidSeedError(OutcomeModelError):
    """The master seed is not a usable integer seed."""


class InvalidOutcomeProbabilityError(OutcomeModelError):
    """A recovery probability is missing, malformed, or outside [0, 1]."""


class MissingGroundTruthError(OutcomeModelError):
    """No hidden recovery probability exists for the requested event/intervention.

    Unknown or missing ground truth is an explicit evaluation-layer error; it
    is never guessed, clamped, or silently replaced with a default.
    """


def _validate_seed(seed: Any) -> int:
    """Return the seed when it is a plain integer, else fail explicitly."""
    if type(seed) is not int:
        raise InvalidSeedError(
            f"outcome model seed must be an integer, got {seed!r}"
        )
    return seed


class HiddenOutcomeModel:
    """Event-specific hidden recovery probabilities, keyed by event_id.

    The probabilities mapping is ``{event_id: {intervention: probability}}``
    where intervention covers EXACTLY the locked taxonomy (including
    ``no_action``). Every stored probability satisfies ``0 <= p <= 1``.
    """

    def __init__(
        self,
        seed: int,
        probabilities: Mapping[str, Mapping[str, float]],
    ) -> None:
        _validate_seed(seed)
        if not isinstance(probabilities, Mapping):
            raise OutcomeModelError("probabilities must be a mapping")
        self._seed = seed

        normalized: dict[str, dict[str, float]] = {}
        for event_id, by_intervention in probabilities.items():
            if not isinstance(event_id, str) or not event_id.strip():
                raise OutcomeModelError(
                    "probabilities contains an invalid event_id"
                )
            if event_id in normalized:
                raise OutcomeModelError(
                    f"probabilities contains duplicate event_id {event_id!r}"
                )
            if not isinstance(by_intervention, Mapping):
                raise InvalidOutcomeProbabilityError(
                    f"event {event_id!r} probabilities must be a mapping"
                )
            extra = set(by_intervention) - set(CANDIDATE_INTERVENTIONS)
            if extra:
                raise InvalidOutcomeProbabilityError(
                    f"event {event_id!r} has untracked interventions "
                    f"{sorted(extra)}"
                )
            missing = set(CANDIDATE_INTERVENTIONS) - set(by_intervention)
            if missing:
                raise InvalidOutcomeProbabilityError(
                    f"event {event_id!r} is missing interventions "
                    f"{sorted(missing)}"
                )
            per_intervention: dict[str, float] = {}
            for intervention in _DRAW_ORDER:
                probability = by_intervention[intervention]
                if (
                    isinstance(probability, bool)
                    or not isinstance(probability, (int, float))
                ):
                    raise InvalidOutcomeProbabilityError(
                        f"probability for {event_id!r}/{intervention!r} must "
                        f"be numeric, got {probability!r}"
                    )
                if not (0.0 <= probability <= 1.0):
                    raise InvalidOutcomeProbabilityError(
                        f"probability for {event_id!r}/{intervention!r} is "
                        f"{probability!r}; probabilities must satisfy "
                        "0 <= p <= 1 (never clamped)"
                    )
                per_intervention[intervention] = float(probability)
            normalized[event_id] = per_intervention

        self._probabilities = normalized

    @property
    def seed(self) -> int:
        """The master seed this model was generated from."""
        return self._seed

    @property
    def event_ids(self) -> frozenset[str]:
        """The event identifiers present in this model."""
        return frozenset(self._probabilities)

    def events(self) -> frozenset[str]:
        """Alias for the set of event identifiers covered by the model."""
        return self.event_ids

    def recovery_probability(self, event_id: str, intervention: str) -> float:
        """Return the hidden recovery probability for an event/intervention.

        A missing event or intervention raises ``MissingGroundTruthError``;
        ground truth is never invented.
        """
        by_intervention = self._probabilities.get(event_id)
        if by_intervention is None:
            raise MissingGroundTruthError(
                f"no hidden ground truth exists for event {event_id!r}"
            )
        probability = by_intervention.get(intervention)
        if probability is None:
            raise MissingGroundTruthError(
                f"no hidden ground truth exists for {event_id!r} intervention "
                f"{intervention!r}"
            )
        return probability

    def to_dict(self) -> dict[str, dict[str, float]]:
        """Serialize the hidden model for reproduction and test inspection."""
        return {
            event_id: dict(by_intervention)
            for event_id, by_intervention in self._probabilities.items()
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, HiddenOutcomeModel):
            return NotImplemented
        return self._seed == other._seed and self.to_dict() == other.to_dict()


def generate_hidden_outcome_model(
    events: Iterable[PaymentEvent],
    seed: int,
) -> HiddenOutcomeModel:
    """Generate the hidden outcome model for an event set from an explicit seed.

    Every event receives its own probability for every locked intervention
    (including ``no_action``). Draws come from a fresh per-event
    ``random.Random(f"{seed}:{event_id}")`` instance, so the model is
    reproducible for (seed, event) independently of ordering and of how many
    other events exist.
    """
    _validate_seed(seed)
    if not isinstance(events, (list, tuple)):
        raise OutcomeModelError("events must be a sequence of PaymentEvent")
    if not events:
        raise OutcomeModelError("events must not be empty")
    for event in events:
        if not isinstance(event, PaymentEvent):
            raise OutcomeModelError(
                f"expected PaymentEvent, got {type(event).__name__}"
            )

    probabilities: dict[str, dict[str, float]] = {}
    for event in events:
        rng = random.Random(f"{seed}:{event.event_id}")
        probabilities[event.event_id] = {
            intervention: rng.random() for intervention in _DRAW_ORDER
        }
    return HiddenOutcomeModel(seed=seed, probabilities=probabilities)
