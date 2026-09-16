# T10 — Temporal Neo4j write operations

**Owner:** Aaryan
**Branch:** `aaryan/phase-1/t10-temporal-neo4j`

The graph schema makes `Entity.entity_id`, per-edge lock keys, and global revision
request IDs unique in the configured Neo4j database. Named graph operations accept
only allowlisted relations and parameter objects; they reject user IDs, missing or
malformed provenance, invalid normalized values, stale or overlapping intervals,
and non-timezone-aware timestamps.

A revision transaction claims a global request receipt, acquires an edge lock,
checks both endpoints, creates the successor, closes the predecessor, and records
the resulting edge ID for exact replay. Invalidations are equally idempotent and
retain their source evidence. Historical reads return all revisions in temporal
order. Curated writes remain rejected until T14 supplies human-accepted evidence.

Verification includes focused adapter tests and a disposable-Neo4j integration
suite for successor closure, invalidation replay, rollback after missing endpoints,
closed-history overlap rejection, and concurrent duplicate requests.
