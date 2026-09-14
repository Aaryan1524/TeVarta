"""Deterministic scoring primitives from the frozen MVP §9.

These functions deliberately operate on observable, already-resolved inputs. They
do not call a model, fetch data, or traverse the graph; later pipeline tasks own
those boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import exp, isfinite
from types import MappingProxyType

MAX_HOPS = 3
PATH_SCORE_FLOOR = 0.05
PUSH_CONFIDENCE_FLOOR = 0.6
FRESHNESS_TIME_CONSTANT_HOURS = 18.0
HOP_FACTORS = MappingProxyType({0: 1.0, 1: 0.9, 2: 0.75, 3: 0.55})
SEVERITY_VALUES = MappingProxyType(
    {"critical": 1.0, "high": 0.8, "moderate": 0.55, "low": 0.3, "routine": 0.15}
)


class ScoringInputError(ValueError):
    """Raised when a primitive receives data outside its observable domain."""


def _finite_number(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ScoringInputError(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ScoringInputError(f"{name} must be a finite number") from error
    if not isfinite(number):
        raise ScoringInputError(f"{name} must be a finite number")
    return number


def _unit_interval(name: str, value: float) -> float:
    value = _finite_number(name, value)
    if not 0.0 <= value <= 1.0:
        raise ScoringInputError(f"{name} must be between 0 and 1")
    return value


def _publisher_group_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScoringInputError("publisher_groups must be a non-negative integer")
    return value


def confidence(
    source_quality_max: float,
    publisher_groups: int,
    directness: float,
    resolution_quality: float,
) -> float:
    """Compute §9.1 confidence from observable evidence only."""
    return (
        0.35 * _unit_interval("source_quality_max", source_quality_max)
        + 0.25 * min(1.0, _publisher_group_count(publisher_groups) / 3)
        + 0.25 * _unit_interval("directness", directness)
        + 0.15 * _unit_interval("resolution_quality", resolution_quality)
    )


def path_confidence(edge_confidences: Iterable[float]) -> float:
    """Return the weakest edge confidence on a non-empty winning path."""
    values = tuple(_unit_interval("edge_confidence", value) for value in edge_confidences)
    if not values:
        raise ScoringInputError("path confidence requires at least one edge")
    return min(values)


def path_score(seed_weight: float, edge_weights: Iterable[float], hops: int) -> float:
    """Compute the exact §9.2 path score with explicit hop factors."""
    if isinstance(hops, bool) or not isinstance(hops, int) or hops not in HOP_FACTORS:
        raise ScoringInputError(f"hops must be an integer from 0 through {MAX_HOPS}")
    weights = tuple(_unit_interval("edge_weight", weight) for weight in edge_weights)
    if len(weights) != hops:
        raise ScoringInputError("hops must equal the number of edge weights")

    product = _unit_interval("seed_weight", seed_weight)
    for weight in weights:
        product *= weight
    return product * HOP_FACTORS[hops]


def passes_path_score_floor(path_score_value: float) -> bool:
    """Apply the §9.2 expansion floor without inflating a weak path's score."""
    return _unit_interval("path_score", path_score_value) >= PATH_SCORE_FLOOR


def severity_value(severity_class: str) -> float:
    """Map the five frozen compile-event severity classes to their fixed values."""
    try:
        return SEVERITY_VALUES[severity_class]
    except (KeyError, TypeError) as error:
        choices = ", ".join(SEVERITY_VALUES)
        raise ScoringInputError(f"severity_class must be one of: {choices}") from error


def corroboration(
    primary_source_strength: float,
    publisher_groups: int,
    average_source_quality: float,
) -> float:
    """Compute §9.3 corroboration without allowing volume to create quality."""
    independent_evidence = min(1.0, _publisher_group_count(publisher_groups) / 3) * _unit_interval(
        "average_source_quality", average_source_quality
    )
    primary_strength = _unit_interval("primary_source_strength", primary_source_strength)
    return max(primary_strength, independent_evidence)


def event_importance(
    severity_class: str,
    primary_source_strength: float,
    publisher_groups: int,
    average_source_quality: float,
) -> float:
    """Compute §9.3 event importance from severity and quality-gated evidence."""
    return severity_value(severity_class) * corroboration(
        primary_source_strength, publisher_groups, average_source_quality
    )


def freshness(hours_since_publish: float) -> float:
    """Compute the §9.4 18-hour exponential freshness term."""
    hours = _finite_number("hours_since_publish", hours_since_publish)
    if hours < 0:
        raise ScoringInputError("hours_since_publish must be non-negative")
    return exp(-hours / FRESHNESS_TIME_CONSTANT_HOURS)


def relevance_score(
    path_score_value: float, importance: float, hours_since_publish: float
) -> float:
    """Compute the additive shipped relevance score from §9.4."""
    return 100 * (
        0.5 * _unit_interval("path_score", path_score_value)
        + 0.3 * _unit_interval("importance", importance)
        + 0.2 * freshness(hours_since_publish)
    )


@dataclass(frozen=True)
class RelevanceComponents:
    """The seven measurements persisted for a feed row; four begin at weight zero."""

    path_score: float
    importance: float
    freshness: float
    portfolio_exposure: float
    source_reliability: float
    novelty: float
    explicit_interest: float


def relevance_components(
    *,
    path_score_value: float,
    importance: float,
    hours_since_publish: float,
    portfolio_exposure: float,
    source_reliability: float,
    novelty: float,
    explicit_interest: float,
) -> RelevanceComponents:
    """Validate and retain all §9.4 measurements without changing shipped weights."""
    return RelevanceComponents(
        path_score=_unit_interval("path_score", path_score_value),
        importance=_unit_interval("importance", importance),
        freshness=freshness(hours_since_publish),
        portfolio_exposure=_unit_interval("portfolio_exposure", portfolio_exposure),
        source_reliability=_unit_interval("source_reliability", source_reliability),
        novelty=_unit_interval("novelty", novelty),
        explicit_interest=_unit_interval("explicit_interest", explicit_interest),
    )


def passes_push_confidence_gate(path_confidence_value: float) -> bool:
    """Apply the §9.5 confidence-only notification safety gate."""
    return _unit_interval("path_confidence", path_confidence_value) >= PUSH_CONFIDENCE_FLOOR
