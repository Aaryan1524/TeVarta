from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.graph_schema import SCHEMA_STATEMENTS, initialize_graph_schema
from app.graph_store import (
    CHECK_ENDPOINTS,
    CLAIM_RECEIPT,
    CLOSE,
    COMPLETE_RECEIPT,
    CREATE,
    HISTORY,
    INVALIDATE,
    INVALIDATE_CURRENT,
    LATEST,
    LOCK_EDGE,
    GraphStore,
    GraphWriteError,
    TemporalEdgeInvalidation,
    TemporalEdgeRevision,
    _invalidation_parameters,
    _revision_parameters,
)

REQUEST_ID = UUID("352dc599-701a-4220-a913-43b0f1f9eb2a")
RELATION = "SUPPLIES"


class FakeResult:
    def __init__(self, single=None, records=()):
        self._single = single
        self._records = records

    def single(self):
        return self._single

    def consume(self):
        return None

    def __iter__(self):
        return iter(self._records)


class FakeTransaction:
    def __init__(self, results):
        self._results = iter(results)
        self.queries = []

    def run(self, query, parameters):
        self.queries.append((query, parameters))
        return next(self._results)


class FakeSchemaDriver:
    def __init__(self):
        self.queries = []

    def execute_query(self, query, *, database_):
        self.queries.append((query, database_))


def revision(**overrides):
    values = {
        "request_id": REQUEST_ID,
        "from_entity_id": "ent_supplier",
        "to_entity_id": "ent_manufacturer",
        "relation_type": RELATION,
        "weight": 0.85,
        "confidence": 0.83,
        "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        "observed_at": datetime(2026, 9, 12, 12, 10, tzinfo=UTC),
        "source_ids": ("art_123",),
        "evidence_count": 1,
        "directness": 1.0,
        "origin": "structured",
    }
    return TemporalEdgeRevision(**(values | overrides))


def invalidation(**overrides):
    values = {
        "request_id": uuid4(),
        "from_entity_id": "ent_supplier",
        "to_entity_id": "ent_manufacturer",
        "relation_type": RELATION,
        "valid_until": datetime(2026, 2, 1, tzinfo=UTC),
        "invalidated_at": datetime(2026, 9, 12, 12, 10, tzinfo=UTC),
        "source_ids": ("art_correction",),
    }
    return TemporalEdgeInvalidation(**(values | overrides))


def test_schema_initialization_creates_required_uniqueness_constraints():
    driver = FakeSchemaDriver()

    initialize_graph_schema(driver, database="tevarta_test")

    assert driver.queries == [(statement, "tevarta_test") for statement in SCHEMA_STATEMENTS]


def test_new_revision_locks_edge_creates_successor_then_closes_prior_interval():
    parameters = _revision_parameters(revision())
    transaction = FakeTransaction(
        [
            FakeResult({"payload_hash": parameters["payload_hash"], "result_revision_id": None}),
            FakeResult(),
            FakeResult({"source_exists": True, "target_exists": True}),
            FakeResult(
                {
                    "revision_id": "prior",
                    "valid_from": datetime(2025, 1, 1, tzinfo=UTC),
                    "valid_until": None,
                }
            ),
            FakeResult({"revision_id": str(REQUEST_ID)}),
            FakeResult(),
            FakeResult(),
        ]
    )

    result = GraphStore._create_revision(transaction, parameters)

    assert result.revision_id == str(REQUEST_ID)
    assert not result.replayed
    assert [query for query, _ in transaction.queries] == [
        CLAIM_RECEIPT,
        LOCK_EDGE,
        CHECK_ENDPOINTS,
        LATEST[RELATION],
        CREATE[RELATION],
        CLOSE[RELATION],
        COMPLETE_RECEIPT,
    ]
    assert parameters["valid_from"] == datetime(2026, 1, 1, tzinfo=UTC)


def test_replayed_request_returns_without_a_second_graph_mutation():
    parameters = _revision_parameters(revision())
    transaction = FakeTransaction(
        [FakeResult({"payload_hash": parameters["payload_hash"], "result_revision_id": "prior"})]
    )

    result = GraphStore._create_revision(transaction, parameters)

    assert result.revision_id == "prior"
    assert result.replayed
    assert [query for query, _ in transaction.queries] == [CLAIM_RECEIPT]


def test_conflicting_request_id_is_rejected_before_mutating_the_graph():
    parameters = _revision_parameters(revision())
    transaction = FakeTransaction(
        [FakeResult({"payload_hash": "other", "result_revision_id": "prior"})]
    )

    with pytest.raises(GraphWriteError, match="request ID conflicts"):
        GraphStore._create_revision(transaction, parameters)

    assert [query for query, _ in transaction.queries] == [CLAIM_RECEIPT]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"origin": "curated"}, "origin is not allowed"),
        ({"source_ids": ()}, "evidence provenance"),
        ({"source_ids": "art_123"}, "evidence provenance"),
        ({"source_ids": iter(["art_123"])}, "evidence provenance"),
        ({"evidence_count": 1.5}, "evidence count"),
        ({"relation_type": "DROP DATABASE"}, "relation type"),
        ({"from_entity_id": "usr_123"}, "never accepts user IDs"),
        ({"valid_from": datetime(2026, 1, 1)}, "timezone-aware"),
    ],
)
def test_revision_validation_blocks_unproven_or_unsafe_graph_writes(overrides, message):
    with pytest.raises(GraphWriteError, match=message):
        _revision_parameters(revision(**overrides))


def test_stale_revision_is_rejected_without_creating_or_closing_edges():
    parameters = _revision_parameters(revision())
    transaction = FakeTransaction(
        [
            FakeResult({"payload_hash": parameters["payload_hash"], "result_revision_id": None}),
            FakeResult(),
            FakeResult({"source_exists": True, "target_exists": True}),
            FakeResult(
                {
                    "revision_id": "newer",
                    "valid_from": datetime(2026, 2, 1, tzinfo=UTC),
                    "valid_until": None,
                }
            ),
        ]
    )

    with pytest.raises(GraphWriteError, match="stale temporal revision"):
        GraphStore._create_revision(transaction, parameters)

    assert [query for query, _ in transaction.queries] == [
        CLAIM_RECEIPT,
        LOCK_EDGE,
        CHECK_ENDPOINTS,
        LATEST[RELATION],
    ]


def test_invalidation_closes_active_interval_and_preserves_its_evidence():
    parameters = _invalidation_parameters(invalidation())
    transaction = FakeTransaction(
        [
            FakeResult({"payload_hash": parameters["payload_hash"], "result_revision_id": None}),
            FakeResult(),
            FakeResult({"source_exists": True, "target_exists": True}),
            FakeResult({"revision_id": "current", "valid_from": datetime(2026, 1, 1, tzinfo=UTC)}),
            FakeResult({"revision_id": "current"}),
            FakeResult(),
        ]
    )

    result = GraphStore._invalidate_edge(transaction, parameters)

    assert result.revision_id == "current"
    assert not result.replayed
    assert [query for query, _ in transaction.queries][-3:-1] == [
        INVALIDATE[RELATION],
        INVALIDATE_CURRENT[RELATION],
    ]
    assert transaction.queries[-2][1]["current_revision_id"] == "current"


def test_history_read_maps_only_presentation_safe_temporal_fields():
    transaction = FakeTransaction(
        [
            FakeResult(
                records=[
                    {
                        "revision_id": "prior",
                        "valid_from": datetime(2025, 1, 1, tzinfo=UTC),
                        "valid_until": datetime(2026, 1, 1, tzinfo=UTC),
                        "invalidated_at": None,
                        "source_ids": ["art_1"],
                    },
                    {
                        "revision_id": "current",
                        "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
                        "valid_until": None,
                        "invalidated_at": None,
                        "source_ids": ["art_2"],
                    },
                ]
            )
        ]
    )

    result = GraphStore._read_history(
        transaction,
        {
            "from_entity_id": "ent_supplier",
            "to_entity_id": "ent_manufacturer",
            "relation_type": RELATION,
        },
    )

    assert [record.revision_id for record in result] == ["prior", "current"]
    assert result[0].source_ids == ("art_1",)
    assert transaction.queries[0][0] == HISTORY[RELATION]
