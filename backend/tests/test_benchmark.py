"""T27 benchmark metrics, agreement, readiness, and grouping hints."""

from datetime import UTC, datetime, timedelta

import pytest
from app.benchmark import (
    BenchmarkError,
    agreement,
    bcubed_f1,
    fragmentation,
    labels_from_rows,
    purity,
    score,
    suggest_groups,
    validate_dataset,
)

# Gold: one filing + two wires (E1), a separate story (E2), a singleton (E3).
GOLD = {"a": "E1", "b": "E1", "c": "E1", "d": "E2", "e": "E2", "f": "E3"}


def test_a_perfect_clustering_scores_perfectly():
    predicted = {"a": "x", "b": "x", "c": "x", "d": "y", "e": "y", "f": "z"}
    assert (purity(predicted, GOLD), fragmentation(predicted, GOLD),
            bcubed_f1(predicted, GOLD)) == (1.0, 0.0, 1.0)


def test_bleeding_two_stories_together_lowers_purity_only():
    predicted = {"a": "x", "b": "x", "c": "x", "d": "x", "e": "x", "f": "z"}
    # Cluster x holds 3 of E1 and 2 of E2: majority 3, plus singleton 1 → 4/6.
    assert purity(predicted, GOLD) == pytest.approx(4 / 6)
    assert fragmentation(predicted, GOLD) == 0.0


def test_splitting_a_story_raises_fragmentation_only():
    # The filing (a) lands apart from its wires (b, c): the duplicate §8.2 must merge.
    predicted = {"a": "x", "b": "w", "c": "w", "d": "y", "e": "y", "f": "z"}
    assert purity(predicted, GOLD) == 1.0
    # E1 splits into 2 clusters (1 extra) of a possible 2; E2 not split (of 1) → 1/3.
    assert fragmentation(predicted, GOLD) == pytest.approx(1 / 3)


def test_all_singletons_are_pure_but_fully_fragmented():
    predicted = {article: article for article in GOLD}
    assert (purity(predicted, GOLD), fragmentation(predicted, GOLD)) == (1.0, 1.0)
    assert bcubed_f1(predicted, GOLD) < 0.8


def test_scoring_requires_the_same_articles():
    with pytest.raises(BenchmarkError, match="different articles"):
        purity({"a": "x"}, GOLD)
    assert set(score({k: v for k, v in GOLD.items()}, GOLD)) >= {
        "purity", "fragmentation", "bcubed_f1"}


def test_agreement_ignores_event_names_and_lists_disputed_articles():
    first = {"a": "quake", "b": "quake", "c": "other"}
    same_grouping = {"a": "E9", "b": "E9", "c": "E1"}
    assert agreement(first, same_grouping).as_dict() == {"pairwise_f1": 1.0,
                                                         "disputed_articles": []}
    differs = {"a": "E1", "b": "E1", "c": "E1"}
    result = agreement(first, differs)
    assert result.disputed_articles == ("a", "b", "c")
    assert result.pairwise_f1 == pytest.approx(2 * 1 / (1 + 3))


def test_label_rows_must_be_complete_and_unique():
    with pytest.raises(BenchmarkError, match="non-empty event"):
        labels_from_rows([{"id": "a", "event": " "}])
    with pytest.raises(BenchmarkError, match="twice"):
        labels_from_rows([{"id": "a", "event": "E"}, {"id": "a", "event": "F"}])


def items(counts):
    rows, index = [], 0
    for case, count in counts.items():
        for _ in range(count):
            index += 1
            rows.append({"id": f"b{index:03d}", "url": f"https://x.org/{index}", "title": "t",
                         "snippet": None, "publisher_group": "p", "source_type": "major_wire",
                         "published_at": "2026-10-13T08:00:00Z", "case": case})
    return rows


def test_the_benchmark_is_ready_only_when_complete():
    complete = items({"filing_then_wires": 20, "syndication": 20, "concurrent": 20,
                      "ambiguous_entity": 10, "stale_document": 10, "ordinary": 120})
    labels = {item["id"]: f"E{index // 2}" for index, item in enumerate(complete)}
    report = validate_dataset(complete, labels, labels, labels)
    assert report["ready"] and report["articles"] == 200
    assert report["agreement"]["pairwise_f1"] == 1.0

    short = validate_dataset(complete[:150], labels, labels, labels)
    assert not short["ready"]
    assert any("needs 200" in error for error in short["errors"])

    no_hard_cases = items({"ordinary": 200})
    plain = {item["id"]: "E" for item in no_hard_cases}
    errors = validate_dataset(no_hard_cases, plain, plain, plain)["errors"]
    assert any(error.startswith("syndication: 0") for error in errors)


def test_suggested_groups_follow_shared_entities_within_72_hours():
    start = datetime(2026, 10, 13, tzinfo=UTC)
    rows = [
        {"id": "filing", "published_at": start, "entity_ids": ["ent_tsm"]},
        {"id": "wire", "published_at": start + timedelta(hours=2), "entity_ids": ["ent_tsm"]},
        {"id": "later", "published_at": start + timedelta(days=5), "entity_ids": ["ent_tsm"]},
        {"id": "other", "published_at": start, "entity_ids": ["ent_aapl"]},
    ]
    groups = suggest_groups(rows)
    assert groups["filing"] == groups["wire"]
    assert len({groups["filing"], groups["later"], groups["other"]}) == 3
