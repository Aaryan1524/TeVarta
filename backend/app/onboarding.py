"""T25 PostgreSQL onboarding. No graph/provider calls occur on the read path.

Expansion requests are a durable handoff, not evidence of a running T23 worker.
T23 must publish validated shared views before construction can become ready.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from app.contracts import (
    EntityCandidate,
    EntityResponse,
    EntitySearchResponse,
    GraphResponse,
    Seed,
    SeedCreateRequest,
    SeedResponse,
    SeedsStatusResponse,
)
from app.entity_resolution import identifier_alias
from app.sec_catalog import CatalogInputError, normalize_alias

# Only unambiguous translations supported by the existing v1 boundary.
# Topic/Product/etc. require a founder decision, not a guessed category.
API_TYPES = {
    "Company": "company", "Security": "security", "Country": "place",
    "Facility": "place", "Commodity": "commodity", "Regulation": "regulator",
}

SEARCHABLE_TYPES = sorted(API_TYPES)
EXPANSION_MAX_ATTEMPTS = 3  # mirrors app.expansion.MAX_ATTEMPTS


class OnboardingError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def api_type(db_type: str) -> str:
    """Translate a database entity type to the v1 API vocabulary used by every response."""
    kind = API_TYPES.get(db_type)
    if kind is None:
        raise OnboardingError(
            409, "unsupported_entity_type", "This entity type is not supported by the v1 contract."
        )
    return kind


def candidate(row: dict[str, Any]) -> EntityCandidate:
    kind = api_type(row["type"])
    return EntityCandidate(
        id=row["entity_id"], name=row["canonical_name"], entity_type=kind,
        ticker=row["ticker"], lifecycle=row["status"],
    )


def prepared_graph(entity_id: str, value: Any) -> GraphResponse:
    try:
        graph = GraphResponse.model_validate(value)
        ids = {node.id for node in graph.nodes}
        if entity_id not in ids or len(ids) != len(graph.nodes):
            raise ValueError("invalid root/nodes")
        if any(edge.source_id not in ids or edge.target_id not in ids for edge in graph.edges):
            raise ValueError("dangling edge")
        return graph
    except (ValidationError, ValueError) as exc:
        raise OnboardingError(
            503, "graph_unavailable", "The prepared graph is temporarily unavailable."
        ) from exc


class PostgresOnboardingStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
                yield connection
        except psycopg.Error as exc:
            raise OnboardingError(
                503, "onboarding_unavailable", "Onboarding is temporarily unavailable."
            ) from exc

    @staticmethod
    def _user(connection: psycopg.Connection[dict[str, Any]], public_id: str) -> UUID:
        # Lock user before entity on writes so account deletion cannot race a seed.
        row = connection.execute(
            "SELECT id FROM users WHERE public_id = %s FOR UPDATE", (public_id,)
        ).fetchone()
        if row is None:
            raise OnboardingError(401, "invalid_auth", "Authentication failed.")
        return row["id"]

    def search(self, query: str) -> EntitySearchResponse:
        try:
            key = normalize_alias(query)
        except CatalogInputError:
            return EntitySearchResponse(query=query, candidates=[], ambiguous=False)
        identifier = identifier_alias(query)
        with self._connect() as connection:
            # Exact aliases retain every candidate; seed_count never chooses for a user.
            # Types v1 cannot represent are excluded rather than failing the search.
            rows = connection.execute(
                """SELECT DISTINCT e.* FROM entities e JOIN entity_aliases a USING(entity_id)
                   WHERE a.alias_norm = %s AND e.type = ANY(%s)
                   ORDER BY e.entity_id""", (identifier or key, SEARCHABLE_TYPES),
            ).fetchall()
            if not rows and identifier:
                rows = connection.execute(
                    """SELECT DISTINCT e.* FROM entities e JOIN entity_aliases a USING(entity_id)
                       WHERE a.alias_norm = %s AND e.type = ANY(%s)
                       ORDER BY e.entity_id""", (key, SEARCHABLE_TYPES),
                ).fetchall()
            exact = bool(rows)
            if not rows:
                # Normalized keys contain only ASCII letters/digits: LIKE metacharacters
                # from user input cannot change the prefix match.
                rows = connection.execute(
                    """SELECT DISTINCT e.* FROM entities e JOIN entity_aliases a USING(entity_id)
                       WHERE (a.alias_norm LIKE %s OR a.alias_norm LIKE %s)
                         AND e.type = ANY(%s)
                       ORDER BY e.canonical_name, e.entity_id LIMIT 20""",
                    (key + "%", "ticker:" + key + "%", SEARCHABLE_TYPES),
                ).fetchall()
        return EntitySearchResponse(
            query=query, candidates=[candidate(row) for row in rows],
            ambiguous=exact and len(rows) > 1,
        )

    def create_seed(self, public_id: str, body: SeedCreateRequest) -> SeedResponse:
        with self._connect() as connection:
            user_id = self._user(connection, public_id)
            entity = connection.execute(
                "SELECT * FROM entities WHERE entity_id = %s FOR UPDATE", (body.entity_id,)
            ).fetchone()
            if entity is None:
                raise OnboardingError(404, "entity_not_found", "The entity was not found.")
            item = candidate(entity)
            inserted = connection.execute(
                """INSERT INTO user_seeds(user_id, entity_id, weight, source)
                   VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING weight, source""",
                (user_id, body.entity_id, body.weight, body.source),
            ).fetchone()
            if inserted is not None:
                connection.execute(
                    "UPDATE entities SET seed_count = seed_count + 1 WHERE entity_id = %s",
                    (body.entity_id,),
                )
                self._record_mutation(connection, user_id, "seed_added")
            saved = inserted or connection.execute(
                "SELECT weight, source FROM user_seeds WHERE user_id = %s AND entity_id = %s",
                (user_id, body.entity_id),
            ).fetchone()
            if saved["source"] not in {"manual", "broker"}:
                raise OnboardingError(
                    409, "unsupported_seed_source", "This seed source is not supported by v1."
                )
            # A durable request survives API restarts and broker outages; T23's dispatcher
            # claims it. Seeding again revives a request that exhausted its retries.
            connection.execute(
                """INSERT INTO entity_expansion_requests(entity_id) SELECT %s
                   WHERE NOT EXISTS (SELECT 1 FROM entity_graph_views v
                                     JOIN entities e USING(entity_id)
                                     WHERE entity_id = %s AND v.status = 'ready'
                                       AND e.status = 'live')
                   ON CONFLICT (entity_id) DO UPDATE
                   SET status = 'pending', attempts = 0, requested_at = now(), last_error = NULL
                   WHERE entity_expansion_requests.status = 'failed'
                     AND entity_expansion_requests.attempts >= %s""",
                (body.entity_id, body.entity_id, EXPANSION_MAX_ATTEMPTS),
            )
            return SeedResponse(
                seed=Seed(entity=item, weight=float(saved["weight"]), source=saved["source"]),
                created=inserted is not None,
            )

    def delete_seed(self, public_id: str, entity_id: str) -> None:
        with self._connect() as connection:
            user_id = self._user(connection, public_id)
            entity = connection.execute(
                "SELECT entity_id FROM entities WHERE entity_id = %s FOR UPDATE", (entity_id,)
            ).fetchone()
            if entity is None:
                return
            deleted = connection.execute(
                """DELETE FROM user_seeds WHERE user_id = %s AND entity_id = %s
                   RETURNING entity_id""", (user_id, entity_id),
            ).fetchone()
            if deleted is not None:
                connection.execute(
                    "UPDATE entities SET seed_count = seed_count - 1 WHERE entity_id = %s",
                    (entity_id,),
                )
                self._record_mutation(connection, user_id, "seed_removed")

    @staticmethod
    def _record_mutation(
        connection: psycopg.Connection[dict[str, Any]], user_id: UUID, action: str
    ) -> None:
        connection.execute(
            """INSERT INTO feedback(user_id, action, idempotency_key, ts)
               VALUES (%s, %s, %s, now())""", (user_id, action, "seed_mutation:" + uuid4().hex),
        )

    def _views(self, public_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            # One statement/snapshot: removing a seed excludes its shared graph view.
            return connection.execute(
                """SELECT e.*, s.created_at, v.status AS view_status, v.graph_json,
                          v.updated_at, r.requested_at
                   FROM user_seeds s JOIN users u ON u.id = s.user_id
                   JOIN entities e USING(entity_id)
                   LEFT JOIN entity_graph_views v USING(entity_id)
                   LEFT JOIN entity_expansion_requests r USING(entity_id)
                   WHERE u.public_id = %s ORDER BY e.entity_id""", (public_id,),
            ).fetchall()

    def status(self, public_id: str) -> SeedsStatusResponse:
        rows = self._views(public_id)
        updated = max(
            (row["updated_at"] or row["requested_at"] or row["created_at"] for row in rows),
            default=datetime.now(UTC),
        )
        if not rows:
            return SeedsStatusResponse(status="not_started", updated_at=updated)
        if any(row["view_status"] == "failed" for row in rows):
            return SeedsStatusResponse(
                status="failed", updated_at=updated, detail="Graph construction failed."
            )
        if all(row["status"] == "live" and row["view_status"] == "ready" for row in rows):
            for row in rows:
                prepared_graph(row["entity_id"], row["graph_json"])
            return SeedsStatusResponse(status="ready", updated_at=updated)
        return SeedsStatusResponse(
            status="building", updated_at=updated, detail="Waiting for graph construction."
        )

    def graph(self, public_id: str) -> GraphResponse:
        rows = self._views(public_id)
        nodes, edges = {}, {}
        # Per-root views overlap (two seeds sharing a supplier) and are republished at
        # different times, so the most recently published view wins a shared node or edge.
        ready = sorted(
            (row for row in rows if row["view_status"] == "ready" and row["status"] == "live"),
            key=lambda row: (row["updated_at"], row["entity_id"]),
        )
        for row in ready:
            graph = prepared_graph(row["entity_id"], row["graph_json"])
            nodes.update((node.id, node.model_dump()) for node in graph.nodes)
            edges.update(
                ((edge.source_id, edge.relation, edge.target_id), edge.model_dump())
                for edge in graph.edges
            )
        # Roots are the user's own seeds; the catalog row outranks any view's copy.
        for row in rows:
            nodes[row["entity_id"]] = {
                "id": row["entity_id"], "name": row["canonical_name"],
                "entity_type": api_type(row["type"]),
            }
        return GraphResponse(
            nodes=[nodes[key] for key in sorted(nodes)],
            edges=[edges[key] for key in sorted(edges)],
        )

    def entity(self, entity_id: str) -> EntityResponse:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT e.*, v.status AS view_status, r.requested_at FROM entities e
                   LEFT JOIN entity_graph_views v USING(entity_id)
                   LEFT JOIN entity_expansion_requests r USING(entity_id) WHERE e.entity_id = %s""",
                (entity_id,),
            ).fetchone()
        if row is None:
            raise OnboardingError(404, "entity_not_found", "The entity was not found.")
        graph_status = (
            "live" if row["status"] == "live" and row["view_status"] == "ready"
            else "building" if row["requested_at"] or row["view_status"] == "building"
            else "catalog"
        )
        return EntityResponse(entity=candidate(row), graph_status=graph_status)
