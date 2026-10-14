"""T23 demand-driven expansion: a first seed makes one reusable, bounded live subgraph.

§6.3: promote the seed to live, add its EDGAR industry, then walk the World Graph
upstream (what can affect the seed) under the frozen bounds, promote every reached
entity to live, and publish the prepared view T25 serves. Traversal is deterministic
Python over one named graph read per hop; no LLM is involved.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.discovery import (
    DiscoveryCandidate,
    SearchProviderNotConfigured,
    SearchRequest,
    SearchService,
)
from app.graph_store import GraphStore, GraphWriteError, TemporalEdgeRevision, UpstreamEdge
from app.onboarding import API_TYPES
from app.operations import SearchBudgetExceeded
from app.scoring import MAX_HOPS, PATH_SCORE_FLOOR, confidence, path_score
from app.sec_catalog import CatalogInputError, normalize_alias
from app.worker import celery_app

logger = logging.getLogger(__name__)

# §6.3 bounds. "Nodes with weight >= 0.7" is read as the weight of the edge into the
# hop-1 node (recorded in decisions.md).
HOP2_MIN_EDGE_WEIGHT = 0.7
HOP3_MIN_PATH_SCORE = 0.15
EXPANSION_SEED_WEIGHT = 1.0  # global graph; a user's seed weight applies at fanout


# --- Bounded traversal ------------------------------------------------------------


@dataclass(frozen=True)
class ReachedEntity:
    entity_id: str
    name: str
    type: str
    hop: int
    path_score: float


@dataclass
class Neighborhood:
    root_id: str
    reached: dict[str, ReachedEntity] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], UpstreamEdge] = field(default_factory=dict)

    @property
    def max_hop(self) -> int:
        return max((entity.hop for entity in self.reached.values()), default=0)


NeighborReader = Callable[[Sequence[str]], Sequence[UpstreamEdge]]


def bounded_upstream(root_id: str, read_neighbors: NeighborReader) -> Neighborhood:
    """Best-path upstream reach from `root_id` under the §6.3 hop and score bounds."""
    result = Neighborhood(root_id)
    # node → edge weights along its best path; the frontier holds only nodes allowed
    # to expand to the next hop.
    frontier: dict[str, tuple[float, ...]] = {root_id: ()}
    expanded = {root_id}
    for hop in range(1, MAX_HOPS + 1):
        candidates: dict[str, tuple[float, ...]] = {}
        for edge in read_neighbors(sorted(frontier)):
            if edge.neighbor_id == root_id or edge.subject_id not in frontier:
                continue
            weights = (*frontier[edge.subject_id], edge.weight)
            score = path_score(EXPANSION_SEED_WEIGHT, weights, hop)
            if score < PATH_SCORE_FLOOR:
                continue
            result.edges[(edge.from_entity_id, edge.relation_type, edge.to_entity_id)] = edge
            best = result.reached.get(edge.neighbor_id)
            if best is None or score > best.path_score:
                result.reached[edge.neighbor_id] = ReachedEntity(
                    edge.neighbor_id, edge.neighbor_name, edge.neighbor_type, hop, score
                )
            may_continue = (
                (hop == 1 and edge.weight >= HOP2_MIN_EDGE_WEIGHT)
                or (hop == 2 and score >= HOP3_MIN_PATH_SCORE)
            )
            if may_continue and edge.neighbor_id not in expanded:
                previous = candidates.get(edge.neighbor_id)
                if previous is None or score > path_score(EXPANSION_SEED_WEIGHT, previous, hop):
                    candidates[edge.neighbor_id] = weights
        frontier = candidates
        expanded.update(candidates)
        if not frontier:
            break
    return result


def view_type(db_type: str) -> str:
    """Views use the v1 API vocabulary; types v1 lacks stay lowercase (pending decision)."""
    return API_TYPES.get(db_type, db_type.lower())


def prepared_view(root_id: str, root_name: str, root_type: str,
                  neighborhood: Neighborhood) -> dict[str, Any]:
    """The per-root GraphResponse T25 serves; deterministic for identical graphs."""
    nodes = {root_id: {"id": root_id, "name": root_name, "entity_type": view_type(root_type)}}
    for entity in neighborhood.reached.values():
        nodes[entity.entity_id] = {"id": entity.entity_id, "name": entity.name,
                                   "entity_type": view_type(entity.type)}
    edges = [
        {"source_id": edge.from_entity_id, "target_id": edge.to_entity_id,
         "relation": edge.relation_type, "confidence": round(edge.confidence, 4)}
        for key, edge in sorted(neighborhood.edges.items())
    ]
    return {"nodes": [nodes[key] for key in sorted(nodes)], "edges": edges}


# --- EDGAR industry ---------------------------------------------------------------


@dataclass(frozen=True)
class Industry:
    sic: str
    description: str

    @property
    def entity_id(self) -> str:
        return f"ent_sic_{self.sic}"

    @property
    def name(self) -> str:
        return html.unescape(self.description).title()


class IndustrySource(Protocol):
    def lookup(self, cik: str) -> Industry | None: ...


class EdgarIndustrySource:
    """SIC code and description from SEC submissions, with the declared User-Agent."""

    URL = "https://data.sec.gov/submissions/CIK{cik}.json"

    def __init__(self, fetcher: Callable[[str, Mapping[str, str]], bytes], user_agent: str):
        self.fetcher, self.user_agent = fetcher, user_agent

    def lookup(self, cik: str) -> Industry | None:
        if not re.fullmatch(r"\d{10}", cik):
            return None
        payload = json.loads(self.fetcher(self.URL.format(cik=cik),
                                          {"User-Agent": self.user_agent}))
        sic, description = str(payload.get("sic") or ""), payload.get("sicDescription")
        if not re.fullmatch(r"\d{4}", sic) or not isinstance(description, str):
            return None
        return Industry(sic, " ".join(description.split()))


# --- Request lifecycle --------------------------------------------------------------

MAX_ATTEMPTS = 3
STALE_RUNNING = timedelta(minutes=15)
RETRY_BACKOFF = timedelta(minutes=5)

CLAIM_REQUESTS = """
UPDATE entity_expansion_requests r
SET status = 'running', claimed_at = now(), attempts = r.attempts + 1
WHERE r.entity_id IN (
    SELECT entity_id FROM entity_expansion_requests
    WHERE status = 'pending'
       OR (status = 'running' AND claimed_at < now() - %(stale)s)
       OR (status = 'failed' AND attempts < %(max_attempts)s
           AND completed_at < now() - %(backoff)s * attempts)
    ORDER BY requested_at, entity_id
    FOR UPDATE SKIP LOCKED
    LIMIT %(limit)s)
RETURNING r.entity_id, r.generation, r.claimed_at
"""


@dataclass(frozen=True)
class Claim:
    """Ownership of one running request: only this claim may finish it."""

    entity_id: str
    generation: int
    claimed_at: datetime


def claim_requests(connection: psycopg.Connection[Any], limit: int) -> list[Claim]:
    """Claim pending, stale, or retryable requests; concurrent claimers never overlap."""
    with connection.transaction():
        rows = connection.execute(
            CLAIM_REQUESTS,
            {"stale": STALE_RUNNING, "max_attempts": MAX_ATTEMPTS,
             "backoff": RETRY_BACKOFF, "limit": limit},
        ).fetchall()
    return sorted((Claim(*row) for row in rows), key=lambda claim: claim.entity_id)


def finish_request(connection: psycopg.Connection[Any], claim: Claim,
                   error: str | None = None) -> str | None:
    """Close the claim: complete, or pending again if a republish arrived mid-run.

    Returns the new status, or None when the claim was reclaimed by another worker
    (a stale owner never overwrites its successor).
    """
    with connection.transaction():
        row = connection.execute(
            """UPDATE entity_expansion_requests
               SET status = CASE WHEN %(error)s::text IS NOT NULL THEN 'failed'
                                 WHEN generation = %(generation)s THEN 'complete'
                                 ELSE 'pending' END,
                   attempts = CASE WHEN %(error)s::text IS NULL
                                    AND generation <> %(generation)s THEN 0
                                   ELSE attempts END,
                   completed_at = now(), last_error = %(error)s
               WHERE entity_id = %(entity_id)s AND status = 'running'
                 AND claimed_at = %(claimed_at)s
               RETURNING status, attempts""",
            {"error": error[:200] if error else None, "generation": claim.generation,
             "entity_id": claim.entity_id, "claimed_at": claim.claimed_at},
        ).fetchone()
        if row is None:
            logger.warning("expansion claim was reclaimed", extra={"entity_id": claim.entity_id})
            return None
        new_status, attempts = row
        if new_status == "failed" and attempts >= MAX_ATTEMPTS:
            # Retries are exhausted. A failed republish keeps the last ready view; only a
            # first build that cannot succeed is shown as failed.
            connection.execute(
                """INSERT INTO entity_graph_views (entity_id, status, graph_json)
                   VALUES (%s, 'failed', NULL)
                   ON CONFLICT (entity_id) DO UPDATE SET status = 'failed', updated_at = now()
                   WHERE entity_graph_views.status <> 'ready'""",
                (claim.entity_id,),
            )
    return new_status


def request_republish(connection: psycopg.Connection[Any],
                      entity_ids: Sequence[str] | None = None) -> int:
    """Re-queue published views (all, or the given roots) after graph changes.

    A request already running gets its generation bumped, so it returns to pending
    when it finishes instead of completing on the graph it read before the change.
    """
    with connection.transaction():
        rows = connection.execute(
            """INSERT INTO entity_expansion_requests (entity_id)
               SELECT entity_id FROM entity_graph_views
               WHERE %(all)s OR entity_id = ANY(%(ids)s)
               ON CONFLICT (entity_id) DO UPDATE
               SET generation = entity_expansion_requests.generation + 1,
                   requested_at = now(),
                   status = CASE WHEN entity_expansion_requests.status = 'running'
                                 THEN 'running' ELSE 'pending' END,
                   attempts = CASE WHEN entity_expansion_requests.status = 'running'
                                   THEN entity_expansion_requests.attempts ELSE 0 END,
                   last_error = CASE WHEN entity_expansion_requests.status = 'running'
                                     THEN entity_expansion_requests.last_error END
               RETURNING entity_id""",
            {"all": entity_ids is None, "ids": list(entity_ids or [])},
        ).fetchall()
    return len(rows)


# --- Expansion --------------------------------------------------------------------


@dataclass(frozen=True)
class ExpansionReport:
    entity_id: str
    reached: int
    edges: int
    max_hop: int
    promoted: int
    industry: str  # 'added' | 'present' | 'none' | 'skipped' | 'unavailable'
    search: str  # 'queued:<n>' | 'skipped' | 'budget_exhausted' | 'failed'

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


class ExpansionError(RuntimeError):
    pass


SEARCH_WINDOW = timedelta(days=30)
SEARCH_MAX_RESULTS = 50
_LEGAL_SUFFIX = re.compile(
    r"[,.]?\s+\b(inc|corp|corporation|co|ltd|limited|plc|llc|holdings?|nv|sa|ag)\b\.?",
    flags=re.IGNORECASE,
)


def search_query(canonical_name: str) -> str:
    """'TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD' → 'TAIWAN SEMICONDUCTOR MANUFACTURING'."""
    previous, query = None, canonical_name
    while previous != query:
        previous, query = query, _LEGAL_SUFFIX.sub("", query).strip(" ,.")
    return query or canonical_name


CandidateSink = Callable[[DiscoveryCandidate], None]


class EntityExpander:
    def __init__(self, connection: psycopg.Connection[Any], graph: GraphStore,
                 industry: IndustrySource | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 search: SearchService | None = None,
                 sink: CandidateSink | None = None) -> None:
        self.connection, self.graph, self.industry, self.now = connection, graph, industry, now
        self.search, self.sink = search, sink

    def expand(self, entity_id: str) -> ExpansionReport:
        row = self.connection.execute(
            "SELECT canonical_name, type, cik, created_at FROM entities WHERE entity_id = %s",
            (entity_id,),
        ).fetchone()
        self.connection.commit()
        if row is None:
            raise ExpansionError("entity is not in the catalog")
        name, entity_type, cik, created_at = row
        self.graph.upsert_entity(entity_id, name, entity_type)
        industry_status = self._add_industry(entity_id, cik, created_at)
        neighborhood = bounded_upstream(entity_id, self.graph.read_upstream_neighbors)
        view = prepared_view(entity_id, name, entity_type, neighborhood)
        live_ids = [entity_id, *sorted(neighborhood.reached)]
        with self.connection.transaction():
            promoted = self.connection.execute(
                """UPDATE entities SET status = 'live'
                   WHERE entity_id = ANY(%s) AND status <> 'live'""",
                (live_ids,),
            ).rowcount
            missing = set(live_ids) - {
                found for (found,) in self.connection.execute(
                    "SELECT entity_id FROM entities WHERE entity_id = ANY(%s)", (live_ids,)
                ).fetchall()
            }
            if missing:
                # Every graph entity must exist in Postgres for tagging and seeding.
                raise ExpansionError(f"graph entities missing from catalog: {sorted(missing)}")
            self.connection.execute(
                """INSERT INTO entity_graph_views (entity_id, status, graph_json, updated_at)
                   VALUES (%s, 'ready', %s, now())
                   ON CONFLICT (entity_id) DO UPDATE
                   SET status = 'ready', graph_json = EXCLUDED.graph_json, updated_at = now()""",
                (entity_id, Jsonb(view)),
            )
        search_status = self._targeted_search(entity_id, name)
        report = ExpansionReport(entity_id, len(neighborhood.reached), len(neighborhood.edges),
                                 neighborhood.max_hop, promoted, industry_status, search_status)
        logger.info("entity expanded", extra=report.as_dict())
        return report

    def _targeted_search(self, entity_id: str, name: str) -> str:
        """§6.3 one targeted sweep over the last 30 days, inside the shared monthly cap."""
        if self.search is None or self.sink is None:
            return "skipped"
        now = self.now()
        request = SearchRequest(uuid4(), search_query(name), now - SEARCH_WINDOW,
                                SEARCH_MAX_RESULTS, (entity_id,))
        try:
            candidates = self.search.search(request, now)
        except SearchBudgetExceeded:
            return "budget_exhausted"
        except Exception:  # search is enrichment; the view is already published
            logger.warning("targeted search failed", extra={"entity_id": entity_id})
            return "failed"
        for candidate in candidates:
            self.sink(candidate)
        return f"queued:{len(candidates)}"

    def _add_industry(self, entity_id: str, cik: str | None, created_at: datetime) -> str:
        if not cik or self.industry is None:
            return "skipped"
        try:
            industry = self.industry.lookup(cik)
        except Exception:  # EDGAR is enrichment; its outage must not block onboarding
            logger.warning("EDGAR industry lookup failed", extra={"entity_id": entity_id})
            return "unavailable"
        if industry is None:
            return "none"
        current = {
            edge.to_entity_id for edge in self.graph.read_upstream_neighbors([entity_id])
            if edge.relation_type == "IN_INDUSTRY"
        }
        if industry.entity_id in current:
            return "present"
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO entities (entity_id, canonical_name, type, status)
                   VALUES (%s, %s, 'Industry', 'catalog') ON CONFLICT (entity_id) DO NOTHING""",
                (industry.entity_id, industry.name),
            )
            try:
                alias = normalize_alias(industry.description)
            except CatalogInputError:
                alias = None
            if alias:
                self.connection.execute(
                    """INSERT INTO entity_aliases (alias_norm, entity_id, source, confidence)
                       VALUES (%s, %s, 'edgar_sic', 0.7) ON CONFLICT DO NOTHING""",
                    (alias, industry.entity_id),
                )
        try:
            self.graph.upsert_entity(industry.entity_id, industry.name, "Industry")
            self._write_industry_edge(entity_id, industry, cik, created_at)
        except GraphWriteError:
            # e.g. an invalidated SIC edge cannot be re-opened with its old valid_from.
            logger.warning("industry edge not written", extra={"entity_id": entity_id})
            return "unavailable"
        return "added"

    def _write_industry_edge(self, entity_id: str, industry: Industry, cik: str,
                             created_at: datetime) -> None:
        self.graph.create_temporal_edge_revision(TemporalEdgeRevision(
            request_id=uuid4(),
            from_entity_id=entity_id, to_entity_id=industry.entity_id,
            relation_type="IN_INDUSTRY", weight=0.20,
            # One SEC filing record, stated, identifier-resolved (§9.1).
            confidence=round(confidence(1.0, 1, 1.0, 1.0), 4),
            # The filer's SIC holds at least since the catalog row; first seen now.
            valid_from=created_at, observed_at=self.now(),
            source_ids=(f"edgar:sic:{cik}",), evidence_count=1, directness=1.0,
            origin="structured",
        ))


def run_claims(connection: psycopg.Connection[Any], expander: EntityExpander,
               claims: Sequence[Claim]) -> list[dict[str, object]]:
    """Expand each claimed entity and close its claim."""
    results = []
    for claim in claims:
        try:
            report = expander.expand(claim.entity_id)
        except Exception as error:
            connection.rollback()
            logger.exception("entity expansion failed", extra={"entity_id": claim.entity_id})
            status = finish_request(connection, claim, type(error).__name__)
            results.append({"entity_id": claim.entity_id, "status": status or "reclaimed"})
            continue
        status = finish_request(connection, claim)
        results.append({**report.as_dict(), "entity_id": claim.entity_id,
                        "status": status or "reclaimed"})
    return results


def dispatch(connection: psycopg.Connection[Any], expander: EntityExpander,
             limit: int = 5) -> list[dict[str, object]]:
    """Claim and run up to `limit` expansions."""
    return run_claims(connection, expander, claim_requests(connection, limit))


# --- Worker wiring ----------------------------------------------------------------


def graph_store_from_env() -> tuple[Any, GraphStore]:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.environ["TEVARTA_NEO4J_URL"],
        auth=(os.getenv("TEVARTA_NEO4J_USER", "neo4j"), os.environ["TEVARTA_NEO4J_PASSWORD"]),
    )
    return driver, GraphStore(driver, database=os.getenv("TEVARTA_NEO4J_DATABASE", "neo4j"))


def industry_source_from_env() -> IndustrySource | None:
    from app.discovery import DiscoveryConfigurationError, declared_user_agent, fetch_url

    try:
        return EdgarIndustrySource(fetch_url, declared_user_agent())
    except DiscoveryConfigurationError:
        return None  # no declared contact: SEC refuses us, so skip enrichment


def search_service_from_env() -> SearchService | None:
    from app.discovery import _configured_search_service

    try:
        return _configured_search_service()
    except SearchProviderNotConfigured:
        return None


def enqueue_candidate(candidate: DiscoveryCandidate) -> None:
    from app.discovery import process_candidate

    process_candidate.delay(candidate.as_payload())


@celery_app.task(name="app.expansion.dispatch_expansions")
def dispatch_expansions() -> list[dict[str, object]]:
    with psycopg.connect(os.environ["TEVARTA_POSTGRES_URL"]) as connection:
        claims = claim_requests(connection, 5)
        if not claims:
            return []  # the common case: no graph connection every 15 seconds
        driver, graph = graph_store_from_env()
        try:
            expander = EntityExpander(connection, graph, industry_source_from_env(),
                                      search=search_service_from_env(),
                                      sink=enqueue_candidate)
            return run_claims(connection, expander, claims)
        finally:
            driver.close()
