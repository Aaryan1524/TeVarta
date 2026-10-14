"""T27 event-clustering benchmark: dataset format, metrics, agreement, collection.

§8: "Tune both thresholds against a hand-labelled set of 200 articles in Week 3.
Measure purity and fragmentation." Labels are human: two annotators label every
article independently, disputes are resolved by both, and only the resolved file is
used for scoring. The collector only proposes candidates; any grouping it suggests is
marked unreviewed and is never a label.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

import psycopg

TARGET_SIZE = 200
CASES = ("filing_then_wires", "syndication", "concurrent", "ambiguous_entity",
         "stale_document", "ordinary")
# Minimum articles per hard case, so the set exercises every §8 failure mode.
CASE_MINIMUMS = {"filing_then_wires": 20, "syndication": 20, "concurrent": 20,
                 "ambiguous_entity": 10, "stale_document": 10}
ITEM_FIELDS = ("id", "url", "title", "snippet", "publisher_group", "source_type",
               "published_at", "case")


class BenchmarkError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors[:10]) + (" …" if len(errors) > 10 else ""))
        self.errors = tuple(errors)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise BenchmarkError([f"{path.name}:{number}: {error.msg}"]) from error
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
                            for row in rows), encoding="utf-8")


# --- Labels ---------------------------------------------------------------------------

Clustering = Mapping[str, str]  # article id → event key


def labels_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    errors = []
    for row in rows:
        article, event = row.get("id"), row.get("event")
        if not isinstance(article, str) or not isinstance(event, str) or not event.strip():
            errors.append(f"label row needs an id and a non-empty event: {row!r}")
        elif article in labels:
            errors.append(f"{article} is labelled twice")
        else:
            labels[article] = event.strip()
    if errors:
        raise BenchmarkError(errors)
    return labels


def _clusters(clustering: Clustering) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = defaultdict(set)
    for article, event in clustering.items():
        groups[event].add(article)
    return groups


# --- Metrics -----------------------------------------------------------------------


def purity(predicted: Clustering, gold: Clustering) -> float:
    """Share of articles in the majority gold event of their predicted cluster."""
    articles = set(gold)
    if set(predicted) != articles:
        raise BenchmarkError(["predicted and gold clusterings cover different articles"])
    if not articles:
        return 1.0
    majority = sum(
        Counter(gold[article] for article in members).most_common(1)[0][1]
        for members in _clusters(predicted).values()
    )
    return majority / len(articles)


def fragmentation(predicted: Clustering, gold: Clustering) -> float:
    """How far gold events are split: Σ(k_e − 1) / Σ(|e| − 1) over events of size ≥ 2.

    0 means every gold event sits in one predicted cluster; 1 means every article of
    every multi-article event landed in its own cluster.
    """
    if set(predicted) != set(gold):
        raise BenchmarkError(["predicted and gold clusterings cover different articles"])
    splits = possible = 0
    for members in _clusters(gold).values():
        if len(members) >= 2:
            splits += len({predicted[article] for article in members}) - 1
            possible += len(members) - 1
    return splits / possible if possible else 0.0


def bcubed_f1(predicted: Clustering, gold: Clustering) -> float:
    """Diagnostic: B-cubed F1 balances over-merging and over-splitting per article."""
    predicted_groups, gold_groups = _clusters(predicted), _clusters(gold)
    precision = recall = 0.0
    for article in gold:
        cluster, event = predicted_groups[predicted[article]], gold_groups[gold[article]]
        overlap = len(cluster & event)
        precision += overlap / len(cluster)
        recall += overlap / len(event)
    if not gold:
        return 1.0
    precision, recall = precision / len(gold), recall / len(gold)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def score(predicted: Clustering, gold: Clustering) -> dict[str, float]:
    return {"purity": round(purity(predicted, gold), 4),
            "fragmentation": round(fragmentation(predicted, gold), 4),
            "bcubed_f1": round(bcubed_f1(predicted, gold), 4),
            "articles": len(gold), "gold_events": len(_clusters(gold)),
            "predicted_clusters": len(_clusters(predicted))}


# --- Agreement ---------------------------------------------------------------------


def _same_event_pairs(clustering: Clustering) -> set[tuple[str, str]]:
    return {pair for members in _clusters(clustering).values()
            for pair in combinations(sorted(members), 2)}


@dataclass(frozen=True)
class Agreement:
    pairwise_f1: float
    disputed_articles: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"pairwise_f1": round(self.pairwise_f1, 4),
                "disputed_articles": list(self.disputed_articles)}


def agreement(first: Clustering, second: Clustering) -> Agreement:
    """Same-event pair agreement and the articles whose co-members differ."""
    if set(first) != set(second):
        missing = sorted(set(first) ^ set(second))
        raise BenchmarkError([f"annotators labelled different articles: {missing[:5]}"])
    a, b = _same_event_pairs(first), _same_event_pairs(second)
    both = len(a & b)
    f1 = 1.0 if not a and not b else (2 * both / (len(a) + len(b)))
    disputed = {article for pair in a ^ b for article in pair}
    return Agreement(f1, tuple(sorted(disputed)))


# --- Readiness ---------------------------------------------------------------------


def validate_dataset(items: Sequence[Mapping[str, Any]], resolved: Clustering,
                     first: Clustering, second: Clustering,
                     target_size: int = TARGET_SIZE) -> dict[str, Any]:
    """Is the benchmark ready for Week 3? Every condition is checked, none assumed."""
    errors: list[str] = []
    ids = [item.get("id") for item in items]
    if len(set(ids)) != len(ids):
        errors.append("duplicate article ids in items")
    for item in items:
        missing = [field for field in ITEM_FIELDS if not item.get(field) and field != "snippet"]
        if missing:
            errors.append(f"{item.get('id')}: missing {missing}")
        if item.get("case") not in CASES:
            errors.append(f"{item.get('id')}: case must be one of {CASES}")
        try:
            datetime.fromisoformat(str(item.get("published_at")).replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{item.get('id')}: published_at is not ISO-8601")
    if len(items) != target_size:
        errors.append(f"{len(items)} articles; the benchmark needs {target_size}")
    cases = Counter(item.get("case") for item in items)
    for case, minimum in CASE_MINIMUMS.items():
        if cases[case] < minimum:
            errors.append(f"{case}: {cases[case]} articles, needs at least {minimum}")
    for name, labels in (("resolved", resolved), ("annotator 1", first),
                         ("annotator 2", second)):
        if set(labels) != set(ids):
            errors.append(f"{name} labels do not cover exactly the benchmark articles")
    report: dict[str, Any] = {"articles": len(items), "cases": dict(sorted(cases.items())),
                              "ready": False}
    if set(first) == set(second) == set(ids):
        report["agreement"] = agreement(first, second).as_dict()
    if errors:
        report["errors"] = errors
        return report
    report["gold_events"] = len(_clusters(resolved))
    report["ready"] = True
    return report


# --- Candidate collection ------------------------------------------------------------


def suggest_groups(rows: Sequence[Mapping[str, Any]],
                   window: timedelta = timedelta(hours=72)) -> dict[str, str]:
    """Unreviewed hint for annotators: union articles sharing an entity within 72h.

    This mirrors the §19 fallback (entity + time window) and is never a label.
    """
    parent = {row["id"]: row["id"] for row in rows}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    by_entity: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        for entity in row.get("entity_ids", []):
            by_entity[entity].append(row)
    for members in by_entity.values():
        members = sorted(members, key=lambda row: row["published_at"])
        for left, right in zip(members, members[1:], strict=False):
            if right["published_at"] - left["published_at"] <= window:
                parent[find(right["id"])] = find(left["id"])
    return {row["id"]: f"suggested-{find(row['id'])}" for row in rows}


def collect_candidates(connection: psycopg.Connection[Any], since: datetime,
                       limit: int = 1000) -> list[dict[str, Any]]:
    """Recent tier-1/2 articles with case hints for annotators to curate down to 200."""
    rows = connection.execute(
        """SELECT a.public_id, a.url, a.title, a.snippet, s.publisher_group, s.tier_value,
                  a.published_at, a.simhash,
                  coalesce(array_agg(DISTINCT ae.entity_id)
                           FILTER (WHERE ae.entity_id IS NOT NULL), '{}'),
                  EXISTS (SELECT 1 FROM resolution_misses m WHERE m.article_id = a.id)
           FROM articles a JOIN sources s ON s.id = a.source_id
           LEFT JOIN article_entities ae ON ae.article_id = a.id
           WHERE a.tier IN (1, 2) AND a.published_at >= %s
           GROUP BY a.id, s.publisher_group, s.tier_value
           ORDER BY a.published_at, a.public_id LIMIT %s""",
        (since, limit),
    ).fetchall()
    candidates = [
        {"id": public_id, "url": url, "title": title, "snippet": snippet,
         "publisher_group": group, "tier_value": float(tier_value),
         "published_at": published_at, "simhash": simhash, "entity_ids": sorted(entities),
         "has_resolution_miss": miss}
        for (public_id, url, title, snippet, group, tier_value, published_at, simhash,
             entities, miss) in rows
    ]
    groups = suggest_groups(candidates)
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_group[groups[candidate["id"]]].append(candidate)
    for candidate in candidates:
        group = by_group[groups[candidate["id"]]]
        candidate["suggested_event"] = groups[candidate["id"]]
        candidate["suggestion_reviewed"] = False
        candidate["case_hint"] = _case_hint(candidate, group)
        candidate["published_at"] = candidate["published_at"].isoformat()
        candidate.pop("simhash")
    return candidates


def _case_hint(candidate: Mapping[str, Any], group: Sequence[Mapping[str, Any]]) -> str:
    if candidate["has_resolution_miss"]:
        return "ambiguous_entity"
    publishers = {member["publisher_group"] for member in group}
    if any(member["tier_value"] >= 1.0 for member in group) and len(group) > 1:
        return "filing_then_wires"
    hashes = [member["simhash"] for member in group if member["simhash"] is not None]
    if len(hashes) != len(set(hashes)) or (len(group) > 1 and len(publishers) > 1
                                           and len({m["title"] for m in group}) < len(group)):
        return "syndication"
    if len(group) >= 3:
        return "concurrent"
    return "ordinary"
