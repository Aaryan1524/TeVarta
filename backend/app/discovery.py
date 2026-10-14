"""T19 metadata-only discovery and shared search-budget controls.

This module intentionally stops before URL dedupe, tagging, tiering, and body
fetching.  Those are T20/T21 responsibilities; discovery only emits the frozen
candidate shape to the next worker boundary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

import certifi
import psycopg
import yaml

from app.operations import SearchBudgetExceeded
from app.worker import celery_app

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


DEFAULT_SOURCES_PATH = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"
DEFAULT_USAGE_LIMITS_PATH = Path(__file__).resolve().parents[2] / "config" / "usage_limits.json"


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


def tls_context() -> ssl.SSLContext:
    """Mozilla's CA bundle (certifi), so verification does not depend on the host OS store.

    The macOS Python trust store rejects valid chains such as the ECB's that
    certifi accepts; production and laptops must verify identically.
    """
    return ssl.create_default_context(cafile=certifi.where())


def fetch_url(url: str, headers: Mapping[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(  # nosec B310 -- curated HTTPS URLs
        request, timeout=20, context=tls_context()
    ) as response:
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


CONTACT_EMAIL = re.compile(r"[^@\s()]+@[^@\s()]+\.[A-Za-z]{2,}")


def declared_user_agent(environ: Mapping[str, str] | None = None) -> str:
    """Build the declared client identity SEC and other publishers require.

    SEC's fair-access policy returns 403 to undeclared automated clients such as
    Python's default User-Agent. The contact is operator configuration, never a
    personal address baked into source.
    """
    values = os.environ if environ is None else environ
    contact = values.get("TEVARTA_HTTP_CONTACT", "").strip()
    if not CONTACT_EMAIL.fullmatch(contact):
        raise DiscoveryConfigurationError(
            "set TEVARTA_HTTP_CONTACT to an operations email before polling active sources"
        )
    return f"TeVarta/1.0 ({contact})"


class FeedPoller:
    def __init__(
        self,
        fetcher: UrlFetcher = fetch_url,
        now: Callable[[], datetime] = utc_now,
        user_agent: str | None = None,
    ) -> None:
        self.fetcher, self.now, self.user_agent = fetcher, now, user_agent

    def poll(self, source: SourceConfig) -> tuple[DiscoveryCandidate, ...]:
        if not source.active:
            return ()
        if not source.feed_url:
            raise DiscoveryConfigurationError(f"active source {source.id} has no feed URL")
        if not self.user_agent:
            raise DiscoveryConfigurationError("active sources require a declared User-Agent")
        content = self.fetcher(
            source.feed_url,
            {
                # Many RSS feeds are served as text/xml or application/xml (EIA returns 406
                # to an Accept header without them).
                "Accept": ("application/atom+xml, application/rss+xml, application/xml, "
                           "text/xml, application/json;q=0.9, */*;q=0.1"),
                "User-Agent": self.user_agent,
            },
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


class BraveSearchProvider:
    """The one permitted SearchProvider (T19): Brave News Search, metadata only."""

    URL = "https://api.search.brave.com/res/v1/news/search"
    provider = "brave"

    def __init__(self, api_key: str, fetcher: UrlFetcher = fetch_url,
                 now: Callable[[], datetime] = utc_now) -> None:
        if not api_key.strip():
            raise SearchProviderNotConfigured("BRAVE_SEARCH_API_KEY is empty")
        self._api_key, self.fetcher, self.now = api_key.strip(), fetcher, now

    def search(self, request: SearchRequest) -> tuple[DiscoveryCandidate, ...]:
        today = self.now().astimezone(UTC).date()
        query = urlencode({
            "q": request.query,
            "count": request.max_results,
            # Day-granular window; exact cut-off applied per result below.
            "freshness": f"{request.published_after.astimezone(UTC).date()}to{today}",
            "safesearch": "off",
            "search_lang": "en",
        })
        content = self.fetcher(
            f"{self.URL}?{query}",
            {"Accept": "application/json", "X-Subscription-Token": self._api_key},
        )
        payload = json.loads(content)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise DiscoveryConfigurationError("Brave response has no results list")
        candidates = []
        for result in results:
            if not isinstance(result, dict):
                continue
            url, title = result.get("url"), result.get("title")
            if not isinstance(url, str) or not isinstance(title, str):
                continue
            meta = result.get("meta_url") if isinstance(result.get("meta_url"), dict) else {}
            host = meta.get("hostname") or urlsplit(url).hostname
            if not isinstance(host, str) or not host:
                continue
            published = _parse_datetime(result.get("page_age"), self.now())
            if published < request.published_after:
                continue
            description = result.get("description")
            candidates.append(DiscoveryCandidate(
                url, title, description if isinstance(description, str) else None,
                host.lower(), published, f"web:{host.lower()}",
            ))
        return tuple(candidates)


def _configured_search_service() -> SearchService:
    """Brave behind the hard monthly cap; fails closed until both are configured."""
    if os.getenv("TEVARTA_SEARCH_PROVIDER", "") != "brave":
        raise SearchProviderNotConfigured("set TEVARTA_SEARCH_PROVIDER=brave to enable search")
    api_key = os.getenv("BRAVE_SEARCH_API_KEY", "")
    if not api_key.strip():
        raise SearchProviderNotConfigured("set BRAVE_SEARCH_API_KEY to enable search")
    limits = json.loads(DEFAULT_USAGE_LIMITS_PATH.read_text())
    budget = PostgresSearchBudgetStore(
        os.environ["TEVARTA_POSTGRES_URL"],
        int(limits["limits"]["search"]["monthly_query_cap"]),
        float(limits["alerts"]["search_usage_ratio"]),
    )
    return SearchService(BraveSearchProvider(api_key), budget)


@celery_app.task(name="app.discovery.process_candidate")
def process_candidate(candidate: dict[str, object]) -> dict[str, object]:
    """T20 metadata stage: dedupe, tag, and tier one candidate. Never fetches a body."""
    from app.ingest import MetadataStage
    from app.quarantine import audit_sampled, body_mode

    with psycopg.connect(os.environ["TEVARTA_POSTGRES_URL"]) as connection:
        result = MetadataStage(connection).process(candidate)
    mode = None if result.duplicate or result.tier is None else body_mode(
        result.tier, result.tier_value, audit_sampled(result.url_hash))
    if mode is not None:
        from app.bodies import process_article

        sampled = audit_sampled(result.url_hash) and result.tier == 3
        process_article.delay(str(result.article_id), mode, sampled)  # T21/T22 body stage
    return {**result.as_payload(), "body_mode": mode}


@celery_app.task(name="app.discovery.poll_sources")
def poll_sources() -> int:
    from app.ingest import sync_sources

    sources_path = Path(os.getenv("TEVARTA_SOURCES_CONFIG", DEFAULT_SOURCES_PATH))
    sources = load_sources(sources_path)
    active = [source for source in sources if source.active]
    if not active:
        return 0
    poller = FeedPoller(user_agent=declared_user_agent())
    # Articles reference `sources` rows, so the registry is mirrored before any candidate.
    with psycopg.connect(os.environ["TEVARTA_POSTGRES_URL"]) as connection:
        sync_sources(connection, sources)
    candidates = poller.poll_active(active)
    for candidate in candidates:
        process_candidate.delay(candidate.as_payload())
    return len(candidates)


@celery_app.task(name="app.discovery.entity_sweep")
def entity_sweep() -> int:
    try:
        service = _configured_search_service()
    except SearchProviderNotConfigured as error:
        logger.info("entity sweep skipped: %s", error)
        return 0
    entities = PostgresSeedEntityRepository(os.environ["TEVARTA_POSTGRES_URL"])
    candidates = EntitySweep(entities, service).run()
    for candidate in candidates:
        process_candidate.delay(candidate.as_payload())
    return len(candidates)
