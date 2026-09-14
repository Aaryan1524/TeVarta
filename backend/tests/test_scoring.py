import json
import math
from pathlib import Path

import pytest
from app.scoring import (
    PATH_SCORE_FLOOR,
    PUSH_CONFIDENCE_FLOOR,
    ScoringInputError,
    confidence,
    corroboration,
    event_importance,
    freshness,
    passes_path_score_floor,
    passes_push_confidence_gate,
    path_confidence,
    path_score,
    relevance_components,
    relevance_score,
    severity_value,
)

GOLDEN_CASES = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/scoring/v1/golden_cases.json").read_text()
)


def test_confidence_matches_the_three_frozen_sanity_cases() -> None:
    assert confidence(1.0, 1, 1.0, 1.0) == pytest.approx(5 / 6)
    assert confidence(0.25, 3, 0.6, 0.7) == pytest.approx(0.5925)
    assert confidence(0.7, 3, 1.0, 1.0) == pytest.approx(0.895)


def test_numerical_golden_cases_are_reproducible() -> None:
    for case in GOLDEN_CASES["confidence"]:
        assert confidence(**case["inputs"]) == pytest.approx(case["expected"])
    for case in GOLDEN_CASES["path_score"]:
        assert path_score(**case["inputs"]) == pytest.approx(case["expected"])
    for case in GOLDEN_CASES["relevance"]:
        assert relevance_score(**case["inputs"]) == pytest.approx(case["expected"])


def test_confidence_independence_saturates_at_three_distinct_publisher_groups() -> None:
    assert confidence(0.7, 3, 1.0, 1.0) == confidence(0.7, 300, 1.0, 1.0)


@pytest.mark.parametrize("hops,factor", [(0, 1.0), (1, 0.9), (2, 0.75), (3, 0.55)])
def test_path_score_uses_the_frozen_explicit_hop_factors(hops: int, factor: float) -> None:
    assert path_score(1.0, [1.0] * hops, hops) == factor


def test_path_score_matches_the_two_hop_mvp_example_before_display_rounding() -> None:
    score = path_score(1.0, [0.9, 0.9], 2)

    assert score == pytest.approx(0.6075)
    assert relevance_score(score, 0.8, 0) == pytest.approx(74.375)


def test_path_score_floor_controls_expansion_without_inflating_a_weak_score() -> None:
    below_floor = path_score(0.049999, [], 0)

    assert below_floor == 0.049999
    assert not passes_path_score_floor(below_floor)
    assert passes_path_score_floor(PATH_SCORE_FLOOR)
    assert path_score(0, [1.0], 1) == 0


@pytest.mark.parametrize(("weights", "hops"), [([], 1), ([0.9], 0), ([0.9], 2), ([1.0] * 4, 4)])
def test_path_score_rejects_an_invalid_path_shape(weights: list[float], hops: int) -> None:
    with pytest.raises(ScoringInputError):
        path_score(1.0, weights, hops)


def test_path_confidence_is_the_weakest_edge() -> None:
    assert path_confidence([0.94, 0.61, 0.8]) == 0.61
    with pytest.raises(ScoringInputError):
        path_confidence([])


def test_importance_quality_gates_independence_and_severity_is_fixed() -> None:
    assert corroboration(1.0, 1, 1.0) == 1.0
    assert corroboration(0.25, 3, 0.25) == 0.25
    assert event_importance("high", 0.7, 3, 0.7) == pytest.approx(0.56)
    assert severity_value("moderate") == 0.55


@pytest.mark.parametrize("severity", ["urgent", "HIGH", "", None])
def test_severity_rejects_non_frozen_classes(severity: object) -> None:
    with pytest.raises(ScoringInputError):
        severity_value(severity)  # type: ignore[arg-type]


def test_relevance_is_additive_and_retains_all_seven_measurements() -> None:
    components = relevance_components(
        path_score_value=0.61,
        importance=0.8,
        hours_since_publish=18,
        portfolio_exposure=0.3,
        source_reliability=0.99,
        novelty=0.2,
        explicit_interest=0.1,
    )

    assert components.freshness == pytest.approx(math.exp(-1))
    assert relevance_score(components.path_score, components.importance, 18) == pytest.approx(
        61.8575888234
    )
    assert components.portfolio_exposure == 0.3
    assert components.source_reliability == 0.99
    assert components.novelty == 0.2
    assert components.explicit_interest == 0.1


def test_confidence_gate_is_inclusive_at_the_frozen_boundary() -> None:
    assert passes_push_confidence_gate(PUSH_CONFIDENCE_FLOOR)
    assert not passes_push_confidence_gate(PUSH_CONFIDENCE_FLOOR - 0.001)


@pytest.mark.parametrize("value", [-0.1, 1.1, math.nan, math.inf, True, 10**1000])
def test_unit_interval_inputs_reject_out_of_range_or_non_finite_values(value: float) -> None:
    with pytest.raises(ScoringInputError):
        confidence(value, 1, 1.0, 1.0)


def test_freshness_rejects_negative_or_non_finite_time() -> None:
    for value in (-1, math.nan, math.inf):
        with pytest.raises(ScoringInputError):
            freshness(value)
