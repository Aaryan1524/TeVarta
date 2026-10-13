"""Load reviewed curated edges into a disposable Neo4j through the named graph boundary."""

import json
from datetime import date
from os import environ
from uuid import uuid4

import pytest
from app.curation import (
    CurationFile,
    ResolvedEndpoint,
    ReviewLog,
    apply_curated_load,
    parse_review_log,
    plan_curated_load,
    review_batch,
)
from app.graph_store import GraphStore
from neo4j import GraphDatabase

pytestmark = pytest.mark.neo4j
EVIDENCE = {
    "url": "https://www.sec.gov/Archives/edgar/data/1046179/tsm-20241231.htm",
    "source_type": "sec_filing",
    "publisher_group": "tsmc",
}


@pytest.fixture(scope="module")
def graph_store():
    uri = environ.get("TEVARTA_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("Set TEVARTA_TEST_NEO4J_URI to run Neo4j integration tests")
    auth = (
        environ.get("TEVARTA_TEST_NEO4J_USER", "neo4j"),
        environ["TEVARTA_TEST_NEO4J_PASSWORD"],
    )
    driver = GraphDatabase.driver(uri, auth=auth)
    driver.verify_connectivity()
    store = GraphStore(driver, database=environ.get("TEVARTA_TEST_NEO4J_DATABASE", "neo4j"))
    store.initialize()
    yield store
    driver.close()


class Resolver:
    def __init__(self):
        # Receipts are global, so each test reviews unique content (a unique SHA).
        self.suffix = suffix = uuid4().hex
        self.endpoints = {
            "TSM": ResolvedEndpoint(f"ent_curation_test_tsm_{suffix}", "TSMC", "Company", 1.0),
            "ASML": ResolvedEndpoint(f"ent_curation_test_asml_{suffix}", "ASML", "Company", 1.0),
        }

    def resolve(self, surface):
        return self.endpoints[surface]


def reviewed(resolver, valid_from="2024-12-31", weight=0.9, proposal_id="tsm-20260928-01",
             log=None, curation=None):
    batch = {
        "batch_id": "tsm-20260928",
        "drafted_by": "human",
        "drafted_at": "2026-09-28T10:00:00+00:00",
        "usage_request_id": None,
        "subject": "TSM",
        "evidence": [EVIDENCE],
        "proposals": [
            {
                "proposal_id": proposal_id,
                "draft": None,
                "draft_sha": None,
                "decision": "accept",
                "reason": None,
                "edge": {
                    "source": "TSM", "relation": "DEPENDS_ON", "target": "ASML",
                    "weight": weight, "directness": 1.0,
                    "rationale": f"Sole EUV lithography supplier ({resolver.suffix})",
                    "evidence": [EVIDENCE], "valid_from": valid_from,
                    "review_by": "2027-03-28",
                },
            }
        ],
    }
    outcome = review_batch(
        batch, curation or CurationFile((), ()), log or ReviewLog(()), "aaryan",
        date(2026, 9, 28),
    )
    records = (log.records if log else ()) + outcome.decided
    return outcome.curation, parse_review_log(json.dumps(r.as_json()) for r in records)


def test_reloading_reviewed_edges_replays_without_duplicating(graph_store):
    resolver = Resolver()
    curation, log = reviewed(resolver)
    plan = plan_curated_load(curation, log, resolver)

    first = apply_curated_load(plan, graph_store)
    second = apply_curated_load(plan_curated_load(curation, log, resolver), graph_store)

    assert [result.status for result in first] == ["created"]
    assert [result.status for result in second] == ["replayed"]
    assert second[0].revision_id == first[0].revision_id
    history = graph_store.read_edge_history(
        resolver.endpoints["TSM"].entity_id, resolver.endpoints["ASML"].entity_id, "DEPENDS_ON"
    )
    assert len(history) == 1
    assert history[0].source_ids == (curation.edges[0].source_id,)
    assert history[0].valid_until is None


def test_reviewed_revision_with_later_validity_supersedes_the_loaded_edge(graph_store):
    resolver = Resolver()
    curation, log = reviewed(resolver)
    apply_curated_load(plan_curated_load(curation, log, resolver), graph_store)
    # Replace the edge in the file with a reviewed revision, as a curator would.
    revised, log = reviewed(resolver, "2025-06-30", 0.95, "tsm-20260928-02", log)

    [result] = apply_curated_load(plan_curated_load(revised, log, resolver), graph_store)

    assert result.status == "created"
    history = graph_store.read_edge_history(
        resolver.endpoints["TSM"].entity_id, resolver.endpoints["ASML"].entity_id, "DEPENDS_ON"
    )
    assert [record.source_ids for record in history] == [
        (curation.edges[0].source_id,), (revised.edges[0].source_id,)
    ]
    assert history[0].valid_until == history[1].valid_from


def test_revision_without_later_validity_fails_with_guidance(graph_store):
    resolver = Resolver()
    curation, log = reviewed(resolver)
    apply_curated_load(plan_curated_load(curation, log, resolver), graph_store)
    revised, log = reviewed(resolver, "2024-12-31", 0.95, "tsm-20260928-02", log)

    [result] = apply_curated_load(plan_curated_load(revised, log, resolver), graph_store)

    assert result.status == "failed"
    assert "needs a valid_from later" in result.error
