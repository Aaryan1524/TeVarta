"""The named, parameterized temporal write boundary for Te Varta's World Graph."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Any
from uuid import UUID

from neo4j import Driver, ManagedTransaction

RELATION_TYPES = frozenset(
    {
        "DEPENDS_ON",
        "SUPPLIES",
        "SOLE_SOURCE_FOR",
        "MANUFACTURES_AT",
        "EXPOSED_TO",
        "CUSTOMER_OF",
        "COMPETES_WITH",
        "PRODUCES",
        "SUBSIDIARY_OF",
        "OPERATED_BY",
        "OPERATES_IN",
        "REGULATED_BY",
        "LISTED_AS",
        "IN_INDUSTRY",
    }
)
ENTITY_TYPES = frozenset(
    {
        "Company",
        "Security",
        "Product",
        "Facility",
        "Commodity",
        "Industry",
        "Country",
        "Person",
        "Regulation",
        "MacroFactor",
        "Topic",
        "Organization",
    }
)
# T14 is the first code path permitted to add human-accepted curated edges.
WRITE_ORIGINS = frozenset({"structured", "extracted"})


class GraphWriteError(ValueError):
    """Stable domain error for unsafe or contradictory graph writes."""


@dataclass(frozen=True)
class TemporalEdgeRevision:
    request_id: UUID
    from_entity_id: str
    to_entity_id: str
    relation_type: str
    weight: float
    confidence: float
    valid_from: datetime
    observed_at: datetime
    source_ids: tuple[str, ...]
    evidence_count: int
    directness: float
    origin: str


@dataclass(frozen=True)
class TemporalEdgeInvalidation:
    request_id: UUID
    from_entity_id: str
    to_entity_id: str
    relation_type: str
    valid_until: datetime
    invalidated_at: datetime
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class TemporalEdgeRecord:
    revision_id: str
    valid_from: datetime
    valid_until: datetime | None
    invalidated_at: datetime | None
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class TemporalWriteResult:
    revision_id: str
    replayed: bool


def _relation_query(template: str, relation: str) -> str:
    return template.replace("{relation}", relation)


UPSERT_ENTITY = (
    "MERGE (e:Entity {entity_id: $entity_id}) "
    "SET e.canonical_name = $canonical_name, e.type = $entity_type"
)
CLAIM_RECEIPT = (
    "MERGE (r:TemporalRevisionReceipt {request_id: $request_id}) "
    "ON CREATE SET r.payload_hash = $payload_hash "
    "RETURN r.payload_hash AS payload_hash, r.result_revision_id AS result_revision_id"
)
COMPLETE_RECEIPT = (
    "MATCH (r:TemporalRevisionReceipt {request_id: $request_id}) "
    "SET r.result_revision_id = $result_revision_id"
)
LOCK_EDGE = (
    "MERGE (l:TemporalEdgeLock {edge_key: $edge_key}) "
    "SET l.last_request_id = $request_id"
)
CHECK_ENDPOINTS = (
    "OPTIONAL MATCH (s:Entity {entity_id: $from_entity_id}) "
    "OPTIONAL MATCH (t:Entity {entity_id: $to_entity_id}) "
    "RETURN s IS NOT NULL AS source_exists, t IS NOT NULL AS target_exists"
)
LATEST_TEMPLATE = (
    "MATCH (s:Entity {entity_id: $from_entity_id})-[e:{relation}]->"
    "(t:Entity {entity_id: $to_entity_id}) "
    "RETURN e.revision_id AS revision_id, e.valid_from AS valid_from, "
    "e.valid_until AS valid_until "
    "ORDER BY e.valid_from DESC LIMIT 1"
)
CREATE_TEMPLATE = (
    "MATCH (s:Entity {entity_id: $from_entity_id}), "
    "(t:Entity {entity_id: $to_entity_id}) "
    "CREATE (s)-[e:{relation} {revision_id: $request_id, weight: $weight, "
    "confidence: $confidence, valid_from: $valid_from, valid_until: null, "
    "observed_at: $observed_at, invalidated_at: null, source_ids: $source_ids, "
    "evidence_count: $evidence_count, directness: $directness, origin: $origin}]->(t) "
    "RETURN e.revision_id AS revision_id"
)
CLOSE_TEMPLATE = (
    "MATCH (:Entity {entity_id: $from_entity_id})-[e:{relation}]->"
    "(:Entity {entity_id: $to_entity_id}) "
    "WHERE e.revision_id <> $request_id AND e.valid_until IS NULL "
    "SET e.valid_until = $valid_from"
)
INVALIDATE_TEMPLATE = (
    "MATCH (:Entity {entity_id: $from_entity_id})-[e:{relation}]->"
    "(:Entity {entity_id: $to_entity_id}) "
    "WHERE e.valid_until IS NULL "
    "RETURN e.revision_id AS revision_id, e.valid_from AS valid_from "
    "ORDER BY e.valid_from DESC LIMIT 1"
)
INVALIDATE_CURRENT_TEMPLATE = (
    "MATCH (:Entity {entity_id: $from_entity_id})-[e:{relation}]->"
    "(:Entity {entity_id: $to_entity_id}) "
    "WHERE e.revision_id = $current_revision_id AND e.valid_until IS NULL "
    "SET e.valid_until = $valid_until, e.invalidated_at = $invalidated_at, "
    "e.invalidation_source_ids = $source_ids "
    "RETURN e.revision_id AS revision_id"
)
HISTORY_TEMPLATE = (
    "MATCH (:Entity {entity_id: $from_entity_id})-[e:{relation}]->"
    "(:Entity {entity_id: $to_entity_id}) "
    "RETURN e.revision_id AS revision_id, e.valid_from AS valid_from, "
    "e.valid_until AS valid_until, e.invalidated_at AS invalidated_at, "
    "e.source_ids AS source_ids ORDER BY e.valid_from ASC"
)
LATEST = MappingProxyType(
    {relation: _relation_query(LATEST_TEMPLATE, relation) for relation in RELATION_TYPES}
)
CREATE = MappingProxyType(
    {relation: _relation_query(CREATE_TEMPLATE, relation) for relation in RELATION_TYPES}
)
CLOSE = MappingProxyType(
    {relation: _relation_query(CLOSE_TEMPLATE, relation) for relation in RELATION_TYPES}
)
INVALIDATE = MappingProxyType(
    {relation: _relation_query(INVALIDATE_TEMPLATE, relation) for relation in RELATION_TYPES}
)
INVALIDATE_CURRENT = MappingProxyType(
    {
        relation: _relation_query(INVALIDATE_CURRENT_TEMPLATE, relation)
        for relation in RELATION_TYPES
    }
)
HISTORY = MappingProxyType(
    {relation: _relation_query(HISTORY_TEMPLATE, relation) for relation in RELATION_TYPES}
)


def _edge_key(from_entity_id: str, relation_type: str, to_entity_id: str) -> str:
    identity = json.dumps(
        (from_entity_id, relation_type, to_entity_id), separators=(",", ":")
    )
    return sha256(identity.encode()).hexdigest()


def _validate_endpoint_pair(from_entity_id: str, to_entity_id: str) -> None:
    if not from_entity_id or not to_entity_id:
        raise GraphWriteError("edge endpoints are required")
    if any(value.startswith("usr_") for value in (from_entity_id, to_entity_id)):
        raise GraphWriteError("World Graph never accepts user IDs")


def _validate_relation(relation_type: str) -> None:
    if relation_type not in RELATION_TYPES:
        raise GraphWriteError("relation type is not allowed")


def _validate_source_ids(source_ids: Sequence[str]) -> None:
    if (
        not isinstance(source_ids, Sequence)
        or isinstance(source_ids, (str, bytes))
        or not source_ids
        or any(not isinstance(source_id, str) or not source_id.strip() for source_id in source_ids)
    ):
        raise GraphWriteError("evidence provenance is required")


def _validate_aware(*timestamps: datetime) -> None:
    if any(timestamp.tzinfo is None for timestamp in timestamps):
        raise GraphWriteError("timestamps must be timezone-aware")


def _claim_parameters(
    request_id: UUID, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    payload_hash = sha256(
        json.dumps(
            {"operation": operation, **payload}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return payload | {"request_id": str(request_id), "payload_hash": payload_hash}


def _revision_parameters(revision: TemporalEdgeRevision) -> dict[str, Any]:
    _validate_endpoint_pair(revision.from_entity_id, revision.to_entity_id)
    _validate_relation(revision.relation_type)
    _validate_source_ids(revision.source_ids)
    _validate_aware(revision.valid_from, revision.observed_at)
    if revision.origin not in WRITE_ORIGINS:
        raise GraphWriteError("origin is not allowed by the active write workflow")
    if (
        isinstance(revision.evidence_count, bool)
        or not isinstance(revision.evidence_count, int)
        or revision.evidence_count < 1
    ):
        raise GraphWriteError("evidence count must be positive")
    for value in (revision.weight, revision.confidence, revision.directness):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise GraphWriteError("normalized values must be between 0 and 1")
    payload = {
        "from_entity_id": revision.from_entity_id,
        "to_entity_id": revision.to_entity_id,
        "relation_type": revision.relation_type,
        "weight": revision.weight,
        "confidence": revision.confidence,
        "valid_from": revision.valid_from,
        "observed_at": revision.observed_at,
        "source_ids": list(revision.source_ids),
        "evidence_count": revision.evidence_count,
        "directness": revision.directness,
        "origin": revision.origin,
        "edge_key": _edge_key(
            revision.from_entity_id, revision.relation_type, revision.to_entity_id
        ),
    }
    hash_payload = payload | {
        "valid_from": revision.valid_from.isoformat(),
        "observed_at": revision.observed_at.isoformat(),
    }
    return _claim_parameters(revision.request_id, "revision", hash_payload) | payload


def _invalidation_parameters(invalidation: TemporalEdgeInvalidation) -> dict[str, Any]:
    _validate_endpoint_pair(invalidation.from_entity_id, invalidation.to_entity_id)
    _validate_relation(invalidation.relation_type)
    _validate_source_ids(invalidation.source_ids)
    _validate_aware(invalidation.valid_until, invalidation.invalidated_at)
    if invalidation.invalidated_at < invalidation.valid_until:
        raise GraphWriteError("invalidation cannot precede the end of validity")
    payload = {
        "from_entity_id": invalidation.from_entity_id,
        "to_entity_id": invalidation.to_entity_id,
        "relation_type": invalidation.relation_type,
        "valid_until": invalidation.valid_until,
        "invalidated_at": invalidation.invalidated_at,
        "source_ids": list(invalidation.source_ids),
        "edge_key": _edge_key(
            invalidation.from_entity_id,
            invalidation.relation_type,
            invalidation.to_entity_id,
        ),
    }
    hash_payload = payload | {
        "valid_until": invalidation.valid_until.isoformat(),
        "invalidated_at": invalidation.invalidated_at.isoformat(),
    }
    return _claim_parameters(invalidation.request_id, "invalidation", hash_payload) | payload


class GraphStore:
    def __init__(self, driver: Driver, database: str = "neo4j") -> None:
        self._driver, self._database = driver, database

    def initialize(self) -> None:
        from app.graph_schema import initialize_graph_schema

        initialize_graph_schema(self._driver, database=self._database)

    def upsert_entity(self, entity_id: str, canonical_name: str, entity_type: str) -> None:
        if (
            entity_type not in ENTITY_TYPES
            or not entity_id
            or entity_id.startswith("usr_")
            or not canonical_name
        ):
            raise GraphWriteError("invalid Entity payload")
        self._driver.execute_query(
            UPSERT_ENTITY,
            entity_id=entity_id,
            canonical_name=canonical_name,
            entity_type=entity_type,
            database_=self._database,
        )

    def create_temporal_edge_revision(
        self, revision: TemporalEdgeRevision
    ) -> TemporalWriteResult:
        parameters = _revision_parameters(revision)
        with self._driver.session(database=self._database) as session:
            return session.execute_write(self._create_revision, parameters)

    def invalidate_temporal_edge(
        self, invalidation: TemporalEdgeInvalidation
    ) -> TemporalWriteResult:
        parameters = _invalidation_parameters(invalidation)
        with self._driver.session(database=self._database) as session:
            return session.execute_write(self._invalidate_edge, parameters)

    def read_edge_history(
        self, from_entity_id: str, to_entity_id: str, relation_type: str
    ) -> list[TemporalEdgeRecord]:
        _validate_endpoint_pair(from_entity_id, to_entity_id)
        _validate_relation(relation_type)
        parameters = {
            "from_entity_id": from_entity_id,
            "to_entity_id": to_entity_id,
            "relation_type": relation_type,
        }
        with self._driver.session(database=self._database) as session:
            return session.execute_read(self._read_history, parameters)

    @staticmethod
    def _claim_request(
        transaction: ManagedTransaction, parameters: dict[str, Any]
    ) -> str | None:
        receipt = transaction.run(CLAIM_RECEIPT, parameters).single()
        if not receipt:
            raise GraphWriteError("could not claim graph write request")
        if receipt["payload_hash"] != parameters["payload_hash"]:
            raise GraphWriteError("request ID conflicts with an existing graph write")
        result_revision_id = receipt["result_revision_id"]
        if result_revision_id is None:
            return None
        return str(result_revision_id)

    @staticmethod
    def _verify_endpoints(
        transaction: ManagedTransaction, parameters: dict[str, Any]
    ) -> None:
        endpoints = transaction.run(CHECK_ENDPOINTS, parameters).single()
        if not endpoints or not endpoints["source_exists"] or not endpoints["target_exists"]:
            raise GraphWriteError("edge endpoint does not exist")

    @classmethod
    def _create_revision(
        cls, transaction: ManagedTransaction, parameters: dict[str, Any]
    ) -> TemporalWriteResult:
        prior_result = cls._claim_request(transaction, parameters)
        if prior_result is not None:
            return TemporalWriteResult(prior_result, replayed=True)
        transaction.run(LOCK_EDGE, parameters).consume()
        cls._verify_endpoints(transaction, parameters)
        latest = transaction.run(LATEST[parameters["relation_type"]], parameters).single()
        if latest and (
            parameters["valid_from"] <= latest["valid_from"]
            or (
                latest["valid_until"] is not None
                and parameters["valid_from"] < latest["valid_until"]
            )
        ):
            raise GraphWriteError("stale temporal revision is not supported")
        created = transaction.run(CREATE[parameters["relation_type"]], parameters).single()
        if not created:
            raise GraphWriteError("could not create temporal edge revision")
        transaction.run(CLOSE[parameters["relation_type"]], parameters).consume()
        transaction.run(
            COMPLETE_RECEIPT,
            parameters | {"result_revision_id": created["revision_id"]},
        ).consume()
        return TemporalWriteResult(created["revision_id"], replayed=False)

    @classmethod
    def _invalidate_edge(
        cls, transaction: ManagedTransaction, parameters: dict[str, Any]
    ) -> TemporalWriteResult:
        prior_result = cls._claim_request(transaction, parameters)
        if prior_result is not None:
            return TemporalWriteResult(prior_result, replayed=True)
        transaction.run(LOCK_EDGE, parameters).consume()
        cls._verify_endpoints(transaction, parameters)
        current = transaction.run(INVALIDATE[parameters["relation_type"]], parameters).single()
        if not current:
            raise GraphWriteError("there is no active temporal edge to invalidate")
        if parameters["valid_until"] <= current["valid_from"]:
            raise GraphWriteError("invalidation must follow the edge validity start")
        invalidated = transaction.run(
            INVALIDATE_CURRENT[parameters["relation_type"]],
            parameters | {"current_revision_id": current["revision_id"]},
        ).single()
        if not invalidated:
            raise GraphWriteError("could not invalidate active temporal edge")
        transaction.run(
            COMPLETE_RECEIPT,
            parameters | {"result_revision_id": invalidated["revision_id"]},
        ).consume()
        return TemporalWriteResult(invalidated["revision_id"], replayed=False)

    @staticmethod
    def _read_history(
        transaction: ManagedTransaction, parameters: dict[str, Any]
    ) -> list[TemporalEdgeRecord]:
        records = transaction.run(HISTORY[parameters["relation_type"]], parameters)
        return [
            TemporalEdgeRecord(
                revision_id=record["revision_id"],
                valid_from=record["valid_from"],
                valid_until=record["valid_until"],
                invalidated_at=record["invalidated_at"],
                source_ids=tuple(record["source_ids"]),
            )
            for record in records
        ]
