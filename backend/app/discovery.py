"""T19 metadata-only discovery and shared search-budget controls.

This module intentionally stops before URL dedupe, tagging, tiering, and body
fetching.  Those are T20/T21 responsibilities; discovery only emits the frozen
candidate shape to the next worker boundary.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import psycopg
import yaml

from app.operations import SearchBudgetExceeded
from app.worker import celery_app

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


DEFAULT_SOURCES_PATH = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"


class DiscoveryConfigurationError(ValueError):
    pass


class SearchProviderNotConfigured(RuntimeError):
    """Raised fail-closed until founders approve and configure the one provider."""


@dataclass(frozen=True)
class DiscoveryCandidate:
    url: str
    title: str
    snippet: str | None
    publisher: str
    published_at: datetime
    source_hint: str

    def as_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["published_at"] = (
            self.published_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
        return payload


@dataclass(frozen=True)
class SourceConfig:
    id: str
    domain: str
    feed_url: str | None
    publisher_group: str
    tier_value: float
    reliability: float
    poll_interval_s: int
    active: bool
    ingestion_mode: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SourceConfig:
        required = {
            "id",
            "domain",
            "feed_url",
            "publisher_group",
            "tier_value",
            "reliability",
            "poll_interval_s",
            "active",
            "ingestion_mode",
        }
        missing = required - set(value)
        if missing:
            raise DiscoveryConfigurationError(f"source is missing fields: {sorted(missing)}")
        source = cls(**{field: value[field] for field in required})  # type: ignore[arg-type]
        if source.active and not source.feed_url:
            raise DiscoveryConfigurationError(f"active source {source.id} has no feed URL")
        if source.poll_interval_s != 300:
            raise DiscoveryConfigurationError(f"source {source.id} must poll every 300 seconds")
        if source.ingestion_mode not in {"rss", "atom", "json_api"} and source.active:
            raise DiscoveryConfigurationError(f"active source {source.id} has unsupported mode")
        return source


def load_sources(path: Path = DEFAULT_SOURCES_PATH) -> tuple[SourceConfig, ...]:
    payload = yaml.safe_load(path.read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise DiscoveryConfigurationError("sources config must contain a sources list")
    if not all(isinstance(item, dict) for item in payload["sources"]):
        raise DiscoveryConfigurationError("each source must be a mapping")
    return tuple(
        SourceConfig.from_mapping(item) for item in payload["sources"] if isinstance(item, dict)
    )


class UrlFetcher(Protocol):
    def __call__(self, url: str, headers: Mapping[str, str]) -> bytes: ...


def fetch_url(url: str, headers: Mapping[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310 -- curated HTTPS URLs
        return response.read()


def _parse_datetime(value: object, fallback: datetime) -> datetime:
    if not isinstance(value, str) or not value.strip():
        return fallback
    normalized = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            result = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return fallback
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    return " ".join(element.text.split()) or None


def _xml_candidates(
    source: SourceConfig, content: bytes, now: datetime
) -> tuple[DiscoveryCandidate, ...]:
    root = ET.fromstring(content)
    entries = root.findall("{http://www.w3.org/2005/Atom}entry") or root.findall(".//item")
    candidates: list[DiscoveryCandidate] = []
    for entry in entries:
        atom = entry.tag.startswith("{http://www.w3.org/2005/Atom}")
        title = _text(
            entry.find("{http://www.w3.org/2005/Atom}title") if atom else entry.find("title")
        )
        if atom:
            links = entry.findall("{http://www.w3.org/2005/Atom}link")
            link = next((item.get("href") for item in links if item.get("href")), None)
            snippet = _text(entry.find("{http://www.w3.org/2005/Atom}summary"))
            published = _text(entry.find("{http://www.w3.org/2005/Atom}published"))
            published = published or _text(entry.find("{http://www.w3.org/2005/Atom}updated"))
        else:
            link = _text(entry.find("link"))
            snippet = _text(entry.find("description"))
            published = _text(entry.find("pubDate"))
        if title and link:
            candidates.append(
                DiscoveryCandidate(
                    link,
                    title,
                    snippet,
                    source.publisher_group,
                    _parse_datetime(published, now),
                    source.id,
                )
            )
    return tuple(candidates)


def _json_candidates(
    source: SourceConfig, content: bytes, now: datetime
) -> tuple[DiscoveryCandidate, ...]:
    payload = json.loads(content)
    records = (
        payload.get("results", payload.get("items", payload))
        if isinstance(payload, dict)
        else payload
    )
    if not isinstance(records, list):
        raise DiscoveryConfigurationError(f"JSON source {source.id} has no result list")
    candidates: list[DiscoveryCandidate] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = record.get("title") or record.get("headline")
        url = record.get("url") or record.get("html_url")
        if isinstance(title, str) and isinstance(url, str):
            candidates.append(
                DiscoveryCandidate(
                    url,
                    title,
                    record.get("description") or record.get("abstract"),
                    source.publisher_group,
                    _parse_datetime(
                        record.get("published_at") or record.get("publication_date"), now
                    ),
                    source.id,
                )
            )
    return tuple(candidates)


class FeedPoller:
    def __init__(
        self, fetcher: UrlFetcher = fetch_url, now: Callable[[], datetime] = utc_now
    ) -> None:
        self.fetcher, self.now = fetcher, now

    def poll(self, source: SourceConfig) -> tuple[DiscoveryCandidate, ...]:
        if not source.active:
            return ()
        if not source.feed_url:
            raise DiscoveryConfigurationError(f"active source {source.id} has no feed URL")
        content = self.fetcher(
            source.feed_url,
            {"Accept": "application/atom+xml, application/rss+xml, application/json"},
        )
        now = self.now()
        if source.ingestion_mode in {"rss", "atom"}:
            return _xml_candidates(source, content, now)
        return _json_candidates(source, content, now)

    def poll_active(self, sources: Iterable[SourceConfig]) -> tuple[DiscoveryCandidate, ...]:
        candidates: list[DiscoveryCandidate] = []
        for source in sources:
            try:
                candidates.extend(self.poll(source))
            except Exception:
                logger.exception("feed poll failed", extra={"source_id": source.id})
        return tuple(candidates)


@dataclass(frozen=True)
class SearchRequest:
    request_id: UUID
    query: str
    published_after: datetime
    max_results: int
    entity_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.query.strip() or not 1 <= self.max_results <= 50:
            raise ValueError("search requests require a query and 1–50 results")
        if self.published_after.tzinfo is None:
            raise ValueError("published_after must be timezone-aware")


class SearchProvider(Protocol):
    def search(self, request: SearchRequest) -> Sequence[DiscoveryCandidate]: ...


@dataclass(frozen=True)
class SearchReservation:
    request_id: UUID
    period_start: date
    used: int
    cap: int
    replayed: bool
    alert_80_percent: bool


class PostgresSearchBudgetStore:
    """Cross-worker cap enforced by a transaction-scoped PostgreSQL advisory lock."""

    def __init__(self, dsn: str, monthly_query_cap: int, alert_ratio: float = 0.8) -> None:
        if monthly_query_cap < 1 or not 0 < alert_ratio <= 1:
            raise ValueError("search budget cap and alert ratio must be positive")
        self.dsn, self.monthly_query_cap, self.alert_ratio = dsn, monthly_query_cap, alert_ratio

    def reserve(self, request_id: UUID, now: datetime) -> SearchReservation:
        period_start = now.astimezone(UTC).date().replace(day=1)
        with psycopg.connect(self.dsn) as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"tevarta-search:{period_start}",)
            )
            existing = connection.execute(
                "SELECT 1 FROM search_query_reservations WHERE request_id = %s", (request_id,)
            ).fetchone()
            used = connection.execute(
                "SELECT count(*) FROM search_query_reservations WHERE period_start = %s",
                (period_start,),
            ).fetchone()[0]
            if existing:
                return SearchReservation(
                    request_id, period_start, used, self.monthly_query_cap, True, False
                )
            if used >= self.monthly_query_cap:
                raise SearchBudgetExceeded("monthly search query cap reached")
            connection.execute(
                "INSERT INTO search_query_reservations (request_id, period_start) VALUES (%s, %s)",
                (request_id, period_start),
            )
            used += 1
            crossed = used >= self.monthly_query_cap * self.alert_ratio
            alerted = False
            if crossed:
                alerted = (
                    connection.execute(
                        """INSERT INTO search_budget_alerts (period_start, alert_name)
                       VALUES (%s, 'search_cap_80_percent')
                       ON CONFLICT DO NOTHING RETURNING 1""",
                        (period_start,),
                    ).fetchone()
                    is not None
                )
            return SearchReservation(
                request_id, period_start, used, self.monthly_query_cap, False, alerted
            )

    def record_usage(self, request_id: UUID, outcome: str, query_count: int) -> None:
        """Persist a redacted invocation receipt; the reservation owns cap accounting."""
        with psycopg.connect(self.dsn) as connection, connection.transaction():
            connection.execute(
                """INSERT INTO usage_records
                   (request_id, component, outcome, quantity, unit, estimated_cost_usd, query_count)
                   VALUES (%s, 'search', %s, %s, 'query', 0, %s)
                   ON CONFLICT (request_id, component) DO NOTHING""",
                (request_id, outcome, query_count, query_count),
            )


class SearchService:
    def __init__(
        self,
        provider: SearchProvider,
        budget: PostgresSearchBudgetStore,
        alert: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.provider, self.budget = provider, budget
        self.alert = alert or (lambda event: logger.warning("%s", event))

    def search(
        self, request: SearchRequest, now: datetime | None = None
    ) -> tuple[DiscoveryCandidate, ...]:
        try:
            reservation = self.budget.reserve(request.request_id, now or utc_now())
        except SearchBudgetExceeded:
            self.budget.record_usage(request.request_id, "blocked", 0)
            raise
        if reservation.replayed:
            raise RuntimeError(
                "search request was already reserved; create a new request id for a retry"
            )
        if reservation.alert_80_percent:
            self.alert(
                {"event": "search_cap_80_percent", "used": reservation.used, "cap": reservation.cap}
            )
        try:
            candidates = tuple(self.provider.search(request))
        except Exception:
            self.budget.record_usage(request.request_id, "failure", 1)
            raise
        self.budget.record_usage(request.request_id, "success", 1)
        return candidates


class SeedEntityRepository(Protocol):
    def top_seeded_entities(self, limit: int) -> Sequence[tuple[str, str]]: ...


class PostgresSeedEntityRepository:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def top_seeded_entities(self, limit: int) -> Sequence[tuple[str, str]]:
        with psycopg.connect(self.dsn) as connection:
            rows = connection.execute(
                """SELECT entity_id, canonical_name FROM entities
                   WHERE seed_count > 0 ORDER BY seed_count DESC, entity_id ASC LIMIT %s""",
                (limit,),
            ).fetchall()
            return tuple(rows)


class EntitySweep:
    def __init__(
        self,
        entities: SeedEntityRepository,
        search: SearchService,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.entities, self.search, self.now = entities, search, now

    def run(self) -> tuple[DiscoveryCandidate, ...]:
        current = self.now()
        candidates: list[DiscoveryCandidate] = []
        for entity_id, name in self.entities.top_seeded_entities(50):
            request = SearchRequest(uuid4(), name, current - timedelta(hours=24), 50, (entity_id,))
            try:
                candidates.extend(self.search.search(request, current))
            except SearchBudgetExceeded:
                logger.info("entity sweep stopped at monthly search budget")
                break
            except Exception:
                logger.exception("entity sweep query failed", extra={"entity_id": entity_id})
        return tuple(candidates)


def _configured_search_service() -> SearchService:
    raise SearchProviderNotConfigured(
        "T19 requires both founders to approve and configure the one SearchProvider"
    )


@celery_app.task(name="app.discovery.process_candidate")
def process_candidate(candidate: dict[str, object]) -> dict[str, object]:
    """Stable T19 handoff; T20 owns dedupe, tagging, and tiering."""
    return candidate


@celery_app.task(name="app.discovery.poll_sources")
def poll_sources() -> int:
    sources_path = Path(os.getenv("TEVARTA_SOURCES_CONFIG", DEFAULT_SOURCES_PATH))
    candidates = FeedPoller().poll_active(load_sources(sources_path))
    for candidate in candidates:
        process_candidate.delay(candidate.as_payload())
    return len(candidates)


@celery_app.task(name="app.discovery.entity_sweep")
def entity_sweep() -> int:
    service = _configured_search_service()
    entities = PostgresSeedEntityRepository(os.environ["TEVARTA_POSTGRES_URL"])
    candidates = EntitySweep(entities, service).run()
    for candidate in candidates:
        process_candidate.delay(candidate.as_payload())
    return len(candidates)
