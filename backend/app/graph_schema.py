"""Named, idempotent Neo4j schema initialization for temporal graph safety."""
# ruff: noqa: E501

from __future__ import annotations

from typing import Protocol


class SchemaDriver(Protocol):
    def execute_query(self, query: str, *, database_: str) -> object: ...


SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE",
    "CREATE CONSTRAINT temporal_edge_lock_unique IF NOT EXISTS FOR (l:TemporalEdgeLock) REQUIRE l.edge_key IS UNIQUE",
    "CREATE CONSTRAINT temporal_revision_receipt_unique IF NOT EXISTS FOR (r:TemporalRevisionReceipt) REQUIRE r.request_id IS UNIQUE",
)


def initialize_graph_schema(driver: SchemaDriver, database: str) -> None:
    """Create the three uniqueness guarantees required by GraphStore transactions."""
    for statement in SCHEMA_STATEMENTS:
        driver.execute_query(statement, database_=database)
