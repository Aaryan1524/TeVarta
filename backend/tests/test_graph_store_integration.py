"""Exercise named temporal graph operations against a disposable Neo4j instance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from os import environ
from uuid import uuid4

import pytest
from app.graph_store import (
    GraphStore,
    GraphWriteError,
    TemporalEdgeInvalidation,
    TemporalEdgeRevision,
)
from neo4j import GraphDatabase

pytestmark = pytest.mark.neo4j


@pytest.fixture(scope="module")
def graph_store():
    uri = environ.get("TEVARTA_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("Set TEVARTA_TEST_NEO4J_URI to run Neo4j integration tests")
    auth = (
        environ.get("TEVARTA_TEST_NEO4J_USER", "neo4j"),
        environ["TEVARTA_TEST_NEO4J_PASSWORD"],
    )
    driver = GraphDatabase.driver(
        uri,
        auth=auth,
    )
    driver.verify_connectivity()
    store = GraphStore(driver, database=environ.get("TEVARTA_TEST_NEO4J_DATABASE", "neo4j"))
    store.initialize()
    yield store
    driver.close()


def entity_pair(store: GraphStore) -> tuple[str, str]:
    suffix = uuid4().hex
    source_id, target_id = f"ent_graph_test_source_{suffix}", f"ent_graph_test_target_{suffix}"
    store.upsert_entity(source_id, "Test Supplier", "Company")
    store.upsert_entity(target_id, "Test Manufacturer", "Company")
    return source_id, target_id


def revision(source_id: str, target_id: str, valid_from: datetime, request_id=None):
    return TemporalEdgeRevision(
        request_id=request_id or uuid4(),
        from_entity_id=source_id,
        to_entity_id=target_id,
        relation_type="SUPPLIES",
        weight=0.85,
        confidence=0.83,
        valid_from=valid_from,
        observed_at=datetime(2026, 9, 15, tzinfo=UTC),
        source_ids=("art_graph_test",),
        evidence_count=1,
        directness=1.0,
        origin="structured",
    )


def test_revision_succession_and_history_are_temporal(graph_store):
    source_id, target_id = entity_pair(graph_store)

    first = graph_store.create_temporal_edge_revision(
        revision(source_id, target_id, datetime(2026, 1, 1, tzinfo=UTC))
    )
    second = graph_store.create_temporal_edge_revision(
        revision(source_id, target_id, datetime(2026, 2, 1, tzinfo=UTC))
    )
    history = graph_store.read_edge_history(source_id, target_id, "SUPPLIES")

    assert not first.replayed and not second.replayed
    assert [record.revision_id for record in history] == [first.revision_id, second.revision_id]
    assert history[0].valid_until == datetime(2026, 2, 1, tzinfo=UTC)
    assert history[1].valid_until is None


def test_invalidation_replay_returns_the_original_edge_revision(graph_store):
    source_id, target_id = entity_pair(graph_store)
    created = graph_store.create_temporal_edge_revision(
        revision(source_id, target_id, datetime(2026, 1, 1, tzinfo=UTC))
    )
    invalidation = TemporalEdgeInvalidation(
        request_id=uuid4(),
        from_entity_id=source_id,
        to_entity_id=target_id,
        relation_type="SUPPLIES",
        valid_until=datetime(2026, 3, 1, tzinfo=UTC),
        invalidated_at=datetime(2026, 9, 15, tzinfo=UTC),
        source_ids=("art_graph_correction",),
    )

    first = graph_store.invalidate_temporal_edge(invalidation)
    replay = graph_store.invalidate_temporal_edge(invalidation)
    history = graph_store.read_edge_history(source_id, target_id, "SUPPLIES")

    assert first.revision_id == created.revision_id
    assert not first.replayed
    assert replay.revision_id == created.revision_id
    assert replay.replayed
    assert history[0].valid_until == datetime(2026, 3, 1, tzinfo=UTC)
    assert history[0].invalidated_at == datetime(2026, 9, 15, tzinfo=UTC)


def test_invalid_write_rolls_back_its_receipt_and_closed_history_cannot_overlap(graph_store):
    source_id, target_id = (f"ent_missing_{uuid4().hex}", f"ent_missing_{uuid4().hex}")
    pending = revision(source_id, target_id, datetime(2026, 1, 1, tzinfo=UTC))

    with pytest.raises(GraphWriteError, match="endpoint does not exist"):
        graph_store.create_temporal_edge_revision(pending)

    graph_store.upsert_entity(source_id, "Test Supplier", "Company")
    graph_store.upsert_entity(target_id, "Test Manufacturer", "Company")
    created = graph_store.create_temporal_edge_revision(pending)
    graph_store.invalidate_temporal_edge(
        TemporalEdgeInvalidation(
            request_id=uuid4(),
            from_entity_id=source_id,
            to_entity_id=target_id,
            relation_type="SUPPLIES",
            valid_until=datetime(2026, 3, 1, tzinfo=UTC),
            invalidated_at=datetime(2026, 9, 15, tzinfo=UTC),
            source_ids=("art_graph_correction",),
        )
    )

    with pytest.raises(GraphWriteError, match="stale temporal revision"):
        graph_store.create_temporal_edge_revision(
            revision(source_id, target_id, datetime(2026, 2, 1, tzinfo=UTC))
        )

    successor = graph_store.create_temporal_edge_revision(
        revision(source_id, target_id, datetime(2026, 3, 1, tzinfo=UTC))
    )
    assert created.revision_id != successor.revision_id


def test_concurrent_duplicate_request_creates_one_edge_and_one_replay(graph_store):
    source_id, target_id = entity_pair(graph_store)
    edge = revision(source_id, target_id, datetime(2026, 1, 1, tzinfo=UTC), request_id=uuid4())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(graph_store.create_temporal_edge_revision, [edge, edge]))

    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].revision_id == results[1].revision_id
    assert len(graph_store.read_edge_history(source_id, target_id, "SUPPLIES")) == 1
