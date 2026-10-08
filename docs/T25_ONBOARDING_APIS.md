# T25 — Persisted onboarding API slice

Owner: Aaditya. Tracking: https://github.com/Aaryan1524/TeVarta/issues/31.

Outcome: authenticated catalog autocomplete, idempotent seed mutations, seed counts,
construction status, and graph/entity reads backed by PostgreSQL. Existing fixture
mode and v1 response models remain unchanged.

Dependencies: T03, T05, T08, T11–T12; T23 remains required for real expansion and
prepared graph publication. This slice does not complete T25 or close Phase 2.

## Acceptance and verification

- Exact identifiers/aliases return candidates; ambiguous aliases require user
  selection. Prefix search returns up to 20 candidates without external search.
- Writes derive identity only from the common T11 session dependency. Seed retries
  return the original weight/source with `created=false`; they do not edit holdings.
- User and entity row locks serialize mutations. Seed count, mutation receipt,
  and durable expansion request commit together or all roll back.
- Graph reads join the current user's seeds to shared prepared views. Removing a
  seed removes that user's access through the seed, preserving other users' overlays
  and shared graph data. Catalog metadata is global and requires authentication.
- Pending work remains `building`. Ready requires both a live entity and a valid
  ready snapshot. Failed construction reports `failed`; invalid snapshots and
  database failures return sanitized error envelopes.
- `backend/tests/test_database.py -k onboarding` exercises real PostgreSQL
  migrations, races, rollback, persistence, two-user session isolation and view
  reads. `backend/tests/test_onboarding.py` checks corrupt snapshots. Existing
  fixture/contract checks verify unchanged OpenAPI.

## Proposed T23 handoff — requires Aaryan/founder review before merge

Migration `0004_onboarding_handoff` adds:

- `entity_expansion_requests(entity_id PRIMARY KEY, requested_at)`: one durable
  request shared globally, containing no user identity. Insertion is the enqueue
  boundary; it does not dispatch a Celery task or assert a worker is running.
- `entity_graph_views(entity_id PRIMARY KEY, status, graph_json, updated_at)`:
  shared per-root prepared data. Status is `building|ready|failed`. Only ready
  rows have JSON; its shape is the existing `GraphResponse`. Nodes use database
  entity type names, which the existing graph-node string field supports.

T23 owns consumption, retry/claim semantics, graph traversal and publication.
Its completion transaction must lock the entity row, set `entities.status=live`,
publish the ready view, update `updated_at`, and acknowledge/delete the durable
request together. Only T23 may promote a catalog entity. A live entity lacking
a prepared view also gets a request; its consumer must reuse the live graph
rather than repeat expansion. Failed work stays durable for T23's retry policy.

Prepared views must contain the root exactly once, unique nodes, existing edge
endpoints, and consistent node/edge values across overlapping views. The API
unions prepared views without traversal. While building, it returns known seed
nodes with no invented edges. Never put user overlays or credentials in views.

## Frozen-contract conflicts and remaining gates

The database supports `Topic`, `Product`, `Industry`, `Person`, `MacroFactor`,
and `Organization`, while `EntityCandidate` allows only five API categories.
This implementation maps Company/Security/Commodity directly, Country/Facility
to place, and Regulation to regulator. Other types return
`409 unsupported_entity_type`; an ambiguous result containing one is never
silently reduced to the representable candidate. Founders must resolve the
taxonomy conflict before all-entity onboarding can be declared complete.

Database seed source `onboarding` also lacks a v1 `Seed.source` representation;
an existing such seed returns `409 unsupported_seed_source` on retry. New API
seeds retain the existing `manual|broker` contract. No broker integration is added.

Server mutation receipts (`seed_added|seed_removed`) are inserted only for actual
writes, with unique receipt keys. T26 should avoid separately emitting the same
successful mutation as another analytics event; UI attempts are not mutations.

Before T25 can close: Aaryan/founder acceptance of the proposed handoff, resolution
of the frozen taxonomy/source conflicts, T23 worker integration and a real
seed → construction → ready graph demo. No provider, scoring or v1 model changed.
