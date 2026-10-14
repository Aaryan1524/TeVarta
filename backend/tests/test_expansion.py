"""T23 bounded upstream traversal and prepared views, without services."""

import pytest
from app.contracts import GraphResponse
from app.expansion import (
    HOP2_MIN_EDGE_WEIGHT,
    HOP3_MIN_PATH_SCORE,
    bounded_upstream,
    prepared_view,
    view_type,
)
from app.graph_store import IMPACT_DIRECTION, UPSTREAM_INCOMING, UPSTREAM_OUTGOING, UpstreamEdge
from app.onboarding import prepared_graph
from app.scoring import PATH_SCORE_FLOOR

TYPES = {"aapl": "Company", "nvda": "Company", "tsm": "Company", "asml": "Company",
         "zeiss": "Company", "taiwan": "Country", "nl": "Country", "sic3674": "Industry",
         "intc": "Company"}


class FakeGraph:
    """Stored edges (from, relation, to, weight); upstream rules mirror the Cypher read."""

    def __init__(self, edges):
        self.edges = edges
        self.reads = []

    def __call__(self, subjects):
        self.reads.append(list(subjects))
        rows = []
        for subject in subjects:
            for source, relation, target, weight in self.edges:
                if source == subject and relation in UPSTREAM_OUTGOING:
                    neighbor = target
                elif target == subject and relation in UPSTREAM_INCOMING:
                    neighbor = source
                else:
                    continue
                rows.append(UpstreamEdge(f"ent_{subject}", f"ent_{neighbor}", neighbor.upper(),
                                         TYPES.get(neighbor, "Company"), f"ent_{source}",
                                         f"ent_{target}", relation, weight, 0.8))
        return rows


def reader(edges):
    graph = FakeGraph(edges)

    def read(subject_ids):
        return graph([subject.removeprefix("ent_") for subject in subject_ids])

    read.graph = graph
    return read


# Shapes of T15 batch 1 (fixture data, not the unaccepted batch itself).
BATCH_SHAPES = [
    ("aapl", "DEPENDS_ON", "tsm", 0.90),
    ("nvda", "DEPENDS_ON", "tsm", 0.90),
    ("tsm", "EXPOSED_TO", "taiwan", 0.80),
    ("asml", "SUPPLIES", "tsm", 0.85),
    ("asml", "EXPOSED_TO", "nl", 0.80),
    ("zeiss", "SOLE_SOURCE_FOR", "asml", 0.95),
    ("aapl", "IN_INDUSTRY", "sic3674", 0.20),
    ("intc", "IN_INDUSTRY", "sic3674", 0.20),
]


def scores(neighborhood):
    return {key.removeprefix("ent_"): (value.hop, round(value.path_score, 4))
            for key, value in neighborhood.reached.items()}


def test_every_relation_has_an_impact_direction():
    assert set(IMPACT_DIRECTION.values()) == {"forward", "reverse", "both"}
    assert "DEPENDS_ON" in UPSTREAM_OUTGOING and "DEPENDS_ON" not in UPSTREAM_INCOMING
    assert "SUPPLIES" in UPSTREAM_INCOMING and "SUPPLIES" not in UPSTREAM_OUTGOING


def test_apple_reaches_its_supply_chain_with_frozen_hop_factors():
    reached = scores(bounded_upstream("ent_aapl", reader(BATCH_SHAPES)))
    assert reached == {
        "tsm": (1, 0.81),          # 0.9 × 0.90
        "sic3674": (1, 0.18),      # 0.2 × 0.90
        "taiwan": (2, 0.54),       # 0.9 × 0.8 × 0.75
        "asml": (2, 0.5737),       # 0.9 × 0.85 × 0.75 = 0.57375
        "nl": (3, 0.3366),         # 0.9 × 0.85 × 0.8 × 0.55
        "zeiss": (3, 0.3997),      # 0.9 × 0.85 × 0.95 × 0.55
    }


def test_co_customers_and_industry_peers_are_not_upstream():
    # NVIDIA also depends on TSMC; Intel shares Apple's industry. Neither affects Apple.
    reached = scores(bounded_upstream("ent_aapl", reader(BATCH_SHAPES)))
    assert "nvda" not in reached and "intc" not in reached


def test_weak_hop_one_edges_are_not_expanded():
    # The industry edge (0.2) is reached but never walked further.
    read = reader(BATCH_SHAPES)
    bounded_upstream("ent_aapl", read)
    assert all("sic3674" not in subjects for subjects in read.graph.reads[1:])


@pytest.mark.parametrize(("weight", "expands"), [(HOP2_MIN_EDGE_WEIGHT, True), (0.69, False)])
def test_hop_two_requires_a_hop_one_edge_of_at_least_point_seven(weight, expands):
    edges = [("root", "DEPENDS_ON", "a", weight), ("a", "DEPENDS_ON", "b", 1.0)]
    assert ("b" in scores(bounded_upstream("ent_root", reader(edges)))) is expands


@pytest.mark.parametrize(("hop2_weight", "expands"), [(0.2, True), (0.19, False)])
def test_hop_three_requires_a_hop_two_path_score_of_point_one_five(hop2_weight, expands):
    # Hop-2 score = 1.0 × w × 0.75: w=0.2 → 0.15 (the bound), w=0.19 → 0.1425.
    edges = [("root", "DEPENDS_ON", "a", 1.0), ("a", "DEPENDS_ON", "b", hop2_weight),
             ("b", "DEPENDS_ON", "c", 1.0)]
    reached = scores(bounded_upstream("ent_root", reader(edges)))
    assert reached["b"][1] >= HOP3_MIN_PATH_SCORE if expands else reached["b"][1] < 0.15
    assert ("c" in reached) is expands


@pytest.mark.parametrize(("weight", "kept"), [(0.056, True), (0.055, False)])
def test_paths_below_the_floor_are_dropped(weight, kept):
    # 0.056 × 0.9 = 0.0504 ≥ 0.05; 0.055 × 0.9 = 0.0495 < 0.05.
    neighborhood = bounded_upstream("ent_root", reader([("root", "EXPOSED_TO", "x", weight)]))
    assert ("x" in scores(neighborhood)) is kept
    assert all(e.path_score >= PATH_SCORE_FLOOR for e in neighborhood.reached.values())


def test_traversal_stops_at_three_hops():
    edges = [(f"n{i}", "DEPENDS_ON", f"n{i + 1}", 1.0) for i in range(6)]
    reached = scores(bounded_upstream("ent_n0", reader(edges)))
    assert sorted(reached) == ["n1", "n2", "n3"]


def test_a_node_keeps_its_best_path():
    edges = [("root", "DEPENDS_ON", "a", 0.9), ("a", "DEPENDS_ON", "x", 0.9),
             ("root", "EXPOSED_TO", "x", 0.3)]
    assert scores(bounded_upstream("ent_root", reader(edges)))["x"] == (2, 0.6075)


def test_traversal_reads_one_frontier_per_hop():
    read = reader(BATCH_SHAPES)
    bounded_upstream("ent_aapl", read)
    assert len(read.graph.reads) == 3


def test_prepared_view_is_contract_shaped_and_deterministic():
    neighborhood = bounded_upstream("ent_aapl", reader(BATCH_SHAPES))
    view = prepared_view("ent_aapl", "Apple Inc.", "Company", neighborhood)
    again = prepared_view("ent_aapl", "Apple Inc.", "Company",
                          bounded_upstream("ent_aapl", reader(BATCH_SHAPES)))

    assert view == again
    GraphResponse.model_validate(view)
    prepared_graph("ent_aapl", view)  # T25's own fail-closed check accepts it
    kinds = {node["id"]: node["entity_type"] for node in view["nodes"]}
    assert (kinds["ent_aapl"], kinds["ent_taiwan"], kinds["ent_sic3674"]) == (
        "company", "place", "industry")
    assert len(view["edges"]) == 6


def test_view_types_use_the_v1_vocabulary_where_it_exists():
    assert [view_type(t) for t in ("Company", "Facility", "Regulation", "MacroFactor")] == [
        "company", "place", "regulator", "macrofactor"]


class FakeSearch:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def search(self, request, now=None):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def targeted(search, sink=None):
    from datetime import UTC, datetime

    from app.expansion import EntityExpander

    now = datetime(2026, 10, 13, tzinfo=UTC)
    sunk = []
    expander = EntityExpander(None, None, now=lambda: now, search=search,
                              sink=sink if sink is not None else sunk.append)
    return expander._targeted_search("ent_tsm", "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"), sunk


def test_targeted_search_covers_thirty_days_and_queues_candidates():
    from datetime import UTC, datetime

    search = FakeSearch(["candidate-a", "candidate-b"])
    status, sunk = targeted(search)

    assert (status, sunk) == ("queued:2", ["candidate-a", "candidate-b"])
    (request,) = search.requests
    assert request.query == "TAIWAN SEMICONDUCTOR MANUFACTURING"
    assert request.published_after == datetime(2026, 9, 13, tzinfo=UTC)
    assert (request.max_results, request.entity_ids) == (50, ("ent_tsm",))


def test_targeted_search_degrades_without_blocking_expansion():
    from app.operations import SearchBudgetExceeded

    assert targeted(None)[0] == "skipped"
    assert targeted(FakeSearch(SearchBudgetExceeded("cap")))[0] == "budget_exhausted"
    assert targeted(FakeSearch(TimeoutError("brave down")))[0] == "failed"
