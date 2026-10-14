"""T20 metadata stage: URL dedupe, title/snippet tagging, and the §7.3 tier gate.

This stage sees discovery metadata only and holds no network client, so no article
body can be requested before the article has a tier (§7.2, change B). T21 owns
bodies. Tagging is deterministic alias matching (§6.4 rungs 1-3); no LLM is involved.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg

from app.discovery import SourceConfig
from app.entity_resolution import ResolutionCandidate, choose_candidate, identifier_alias
from app.sec_catalog import CatalogInputError, normalize_alias

logger = logging.getLogger(__name__)


class IngestInputError(ValueError):
    """Raised for a candidate the metadata stage cannot accept."""


# --- URL identity -----------------------------------------------------------------

# Parameters that identify a campaign or click, never the document.
TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z0-9_]+|gclid|dclid|fbclid|msclkid|mc_cid|mc_eid|igshid|ocid|cmpid|"
    r"ref|ref_src|smid|sr_share|_ga|_gl)$",
    flags=re.IGNORECASE,
)


def normalize_url(url: str) -> str:
    """Canonical form for exact dedupe: one document, one key, across tracking noise."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise IngestInputError("article URL must be an absolute http(s) URL")
    host = parts.hostname.lower().rstrip(".")
    host = host.removeprefix("www.")
    try:
        port = parts.port
    except ValueError as error:
        raise IngestInputError("article URL has an invalid port") from error
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not TRACKING_PARAMS.match(key)
        )
    )
    return urlunsplit(("https", netloc, path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def clean_text(value: object, limit: int = 2000) -> str | None:
    """Feed titles and snippets arrive as HTML fragments; keep plain metadata text."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = re.sub(r"<[^>]+>", " ", html.unescape(text))
    text = " ".join(text.split())
    return text[:limit] or None


# --- Mention extraction -----------------------------------------------------------

MAX_NGRAM = 6
# Lowercase words allowed inside a multi-word name ("Bank of America"), never at its edge.
CONNECTORS = frozenset({"of", "and", "&", "the", "de", "for", "du", "la", "le", "von", "und"})
_SEGMENT_BREAK = re.compile(r"[,;:!?()\[\]{}\"“”|–—]|\s[-/]\s|\.(?=\s|$)")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'’.\-]*")
_IDENTIFIER = re.compile(
    r"\$[A-Za-z][A-Za-z0-9.\-]{0,9}"
    r"|\b[A-Za-z]{2,12}\s*:\s*[A-Za-z0-9][A-Za-z0-9.\-/]{0,11}"
    r"|\b[A-Za-z0-9]{1,10}\.[A-Z]{1,3}\b"
    r"|\b(?i:cik)\s*[:#-]?\s*\d{1,10}\b"
)
# SEC filing titles carry the filer's CIK in parentheses: "8-K - APPLE INC (0000320193)".
_SEC_FILER_CIK = re.compile(r"\((\d{10})\)")


@dataclass(frozen=True)
class Mention:
    field: str  # "title" | "snippet"
    segment: int
    start: int
    end: int  # exclusive word index
    surface: str
    key: str
    identifier: bool


def _words(segment: str) -> list[str]:
    words = []
    for word in _WORD.findall(segment):
        word = re.sub(r"['’]s$", "", word).rstrip(".-'’")
        if word:
            words.append(word)
    return words


def _proper(word: str) -> bool:
    return word[0].isupper() or word[0].isdigit()


def _alias_mentions(field: str, text: str) -> list[Mention]:
    mentions = []
    for segment_index, segment in enumerate(_SEGMENT_BREAK.split(text)):
        words = _words(segment)
        for size in range(min(MAX_NGRAM, len(words)), 0, -1):
            for start in range(len(words) - size + 1):
                span = words[start : start + size]
                # A name is a proper-noun span: "inflation target" is not Target Corp.
                if span[0].lower() in CONNECTORS or span[-1].lower() in CONNECTORS:
                    continue
                if not all(_proper(word) or word.lower() in CONNECTORS for word in span):
                    continue
                surface = " ".join(span)
                try:
                    key = normalize_alias(surface)
                except CatalogInputError:
                    continue
                mentions.append(
                    Mention(field, segment_index, start, start + size, surface, key, False)
                )
    return mentions


def extract_mentions(title: str, snippet: str | None, source_domain: str) -> list[Mention]:
    """All candidate surface forms with their lookup keys, identifiers first."""
    mentions: list[Mention] = []
    fields = (("title", title), ("snippet", snippet or ""))
    for field, text in fields:
        spans = []
        for match in _IDENTIFIER.finditer(text):
            key = identifier_alias(match.group(0), bare=False)
            if key:
                spans.append((match.start(), match.end()))
                mentions.append(Mention(field, -1, match.start(), match.end(),
                                        match.group(0), key, True))
        if source_domain == "sec.gov":
            for match in _SEC_FILER_CIK.finditer(text):
                spans.append((match.start(), match.end()))
                mentions.append(Mention(field, -1, match.start(), match.end(),
                                        match.group(0), f"cik:{match.group(1)}", True))
        # Text consumed by an identifier is not also a name: "NASDAQ: AAPL" is not Nasdaq Inc.
        masked = text
        for start, end in sorted(spans, reverse=True):
            masked = masked[:start] + " ; " + masked[end:]
        mentions.extend(_alias_mentions(field, masked))
    return mentions


# --- Alias lookup and tagging -----------------------------------------------------


@dataclass(frozen=True)
class AliasHit:
    entity_id: str
    canonical_name: str
    seed_count: int
    status: str  # 'catalog' | 'live'
    is_identifier: bool


class AliasIndex(Protocol):
    def lookup(self, keys: Collection[str]) -> Mapping[str, Sequence[AliasHit]]: ...


@dataclass(frozen=True)
class TaggedEntity:
    entity_id: str
    status: str
    mentions: int
    salience: float
    in_title: bool


@dataclass(frozen=True)
class Ambiguity:
    surface: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class TaggingResult:
    entities: tuple[TaggedEntity, ...]
    ambiguous: tuple[Ambiguity, ...]

    @property
    def tier(self) -> int:
        return assign_tier(entity.status for entity in self.entities)


def assign_tier(statuses: Iterable[str]) -> int:
    """§7.3: a live entity of any type → 1; catalog only → 2; no match → 3."""
    statuses = set(statuses)
    if "live" in statuses:
        return 1
    return 2 if statuses else 3


# Title mentions are what the article is about; snippet-only mentions are context.
TITLE_SALIENCE = 1.0
SNIPPET_SALIENCE = 0.6


class MetadataTagger:
    def __init__(self, index: AliasIndex) -> None:
        self.index = index

    def tag(self, title: str, snippet: str | None, source_domain: str = "") -> TaggingResult:
        mentions = extract_mentions(title, snippet, source_domain)
        hits = self.index.lookup({mention.key for mention in mentions})
        resolved: dict[str, list[Mention]] = {}
        statuses: dict[str, str] = {}

        def accept(entity_id: str, status: str, mention: Mention) -> None:
            resolved.setdefault(entity_id, []).append(mention)
            statuses[entity_id] = status

        # Rung 1: identifiers are 1:1 by the partial unique index.
        for mention in (m for m in mentions if m.identifier):
            for hit in hits.get(mention.key, ()):
                accept(hit.entity_id, hit.status, mention)

        # Rungs 2-3: longest non-overlapping matched spans per segment.
        taken: dict[tuple[str, int], list[tuple[int, int]]] = {}
        deferred: list[tuple[Mention, Sequence[AliasHit]]] = []
        for mention in sorted(
            (m for m in mentions if not m.identifier and hits.get(m.key)),
            key=lambda m: (m.start - m.end, m.field, m.segment, m.start),
        ):
            spans = taken.setdefault((mention.field, mention.segment), [])
            if any(mention.start < end and start < mention.end for start, end in spans):
                continue
            spans.append((mention.start, mention.end))
            candidates = hits[mention.key]
            if len({hit.entity_id for hit in candidates}) == 1:
                accept(candidates[0].entity_id, candidates[0].status, mention)
            else:
                deferred.append((mention, candidates))

        # §6.4: co-occurrence with entities already tagged, then seed_count; else none.
        ambiguous = []
        for mention, candidates in deferred:
            cooccurrence = {entity_id: len(found) for entity_id, found in resolved.items()}
            options = {hit.entity_id: hit for hit in candidates}
            choice = choose_candidate(
                (ResolutionCandidate(hit.entity_id, hit.canonical_name, hit.seed_count)
                 for hit in options.values()),
                cooccurrence,
            )
            if choice.entity_id is None:
                ambiguous.append(Ambiguity(mention.surface, tuple(sorted(options))))
            else:
                accept(choice.entity_id, options[choice.entity_id].status, mention)

        entities = tuple(
            TaggedEntity(
                entity_id=entity_id,
                status=statuses[entity_id],
                mentions=len(found),
                salience=TITLE_SALIENCE if any(m.field == "title" for m in found)
                else SNIPPET_SALIENCE,
                in_title=any(m.field == "title" for m in found),
            )
            for entity_id, found in sorted(resolved.items())
        )
        return TaggingResult(entities, tuple(ambiguous))


class PostgresAliasIndex:
    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self.connection = connection

    def lookup(self, keys: Collection[str]) -> Mapping[str, Sequence[AliasHit]]:
        if not keys:
            return {}
        rows = self.connection.execute(
            """SELECT a.alias_norm, e.entity_id, e.canonical_name, e.seed_count, e.status,
                      a.is_identifier
               FROM entity_aliases a JOIN entities e USING (entity_id)
               WHERE a.alias_norm = ANY(%s)
               ORDER BY a.alias_norm, e.entity_id""",
            (sorted(keys),),
        ).fetchall()
        result: dict[str, list[AliasHit]] = {}
        for alias, *hit in rows:
            result.setdefault(alias, []).append(AliasHit(*hit))
        return result


# --- Sources ----------------------------------------------------------------------

# Publishers reached only through search are unvetted: they enter as aggregator tier
# (§9.3 0.25) with their own publisher group until a founder classifies them.
WEB_SOURCE_PREFIX = "web:"
WEB_SOURCE_TIER = 0.25
WEB_SOURCE_RELIABILITY = 0.5
WEB_SOURCE_POLL_S = 86400


def sync_sources(connection: psycopg.Connection[Any], sources: Iterable[SourceConfig]) -> int:
    """Mirror config/sources.yaml into `sources`; articles reference these rows."""
    count = 0
    for source in sources:
        connection.execute(
            """INSERT INTO sources (id, domain, feed_url, publisher_group, tier_value,
                                    reliability, poll_interval_s, active)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   domain = EXCLUDED.domain, feed_url = EXCLUDED.feed_url,
                   publisher_group = EXCLUDED.publisher_group,
                   tier_value = EXCLUDED.tier_value, reliability = EXCLUDED.reliability,
                   poll_interval_s = EXCLUDED.poll_interval_s, active = EXCLUDED.active""",
            (source.id, source.domain, source.feed_url, source.publisher_group,
             source.tier_value, source.reliability, source.poll_interval_s, source.active),
        )
        count += 1
    return count


@dataclass(frozen=True)
class SourceRow:
    id: str
    domain: str
    publisher_group: str
    tier_value: float


def resolve_source(connection: psycopg.Connection[Any], source_hint: str) -> SourceRow:
    """Feed candidates name their source; search candidates name a web host."""
    row = connection.execute(
        "SELECT id, domain, publisher_group, tier_value FROM sources WHERE id = %s",
        (source_hint,),
    ).fetchone()
    if row:
        return SourceRow(row[0], row[1], row[2], float(row[3]))
    if not source_hint.startswith(WEB_SOURCE_PREFIX):
        raise IngestInputError(f"unknown source {source_hint!r}; sync sources first")
    host = source_hint.removeprefix(WEB_SOURCE_PREFIX).lower().removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        raise IngestInputError("web source hint must name a host")
    # A host under a configured source's domain is that publisher (reuters.com/x).
    known = connection.execute(
        """SELECT id, domain, publisher_group, tier_value FROM sources
           WHERE %s = domain OR %s LIKE '%%.' || domain
           ORDER BY length(domain) DESC, id LIMIT 1""",
        (host, host),
    ).fetchone()
    if known:
        return SourceRow(known[0], known[1], known[2], float(known[3]))
    source_id = WEB_SOURCE_PREFIX + host
    connection.execute(
        """INSERT INTO sources (id, domain, feed_url, publisher_group, tier_value,
                                reliability, poll_interval_s, active)
           VALUES (%s, %s, NULL, %s, %s, %s, %s, false) ON CONFLICT (id) DO NOTHING""",
        (source_id, host, host, WEB_SOURCE_TIER, WEB_SOURCE_RELIABILITY, WEB_SOURCE_POLL_S),
    )
    return SourceRow(source_id, host, host, WEB_SOURCE_TIER)


# --- The stage ----------------------------------------------------------------------


@dataclass(frozen=True)
class MetadataResult:
    article_id: UUID | None
    public_id: str | None
    duplicate: bool
    tier: int | None
    source_id: str
    tier_value: float
    entity_ids: tuple[str, ...]
    url_hash: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "article_id": str(self.article_id) if self.article_id else None,
            "public_id": self.public_id,
            "duplicate": self.duplicate,
            "tier": self.tier,
            "source_id": self.source_id,
            "tier_value": self.tier_value,
            "entity_ids": list(self.entity_ids),
        }


def _published_at(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise IngestInputError("candidate published_at must be a timestamp")
    if parsed.tzinfo is None:
        raise IngestInputError("candidate published_at must be timezone-aware")
    return parsed.astimezone(UTC)


class MetadataStage:
    """Discovery candidate → deduped, tagged, tiered `articles` row. Never fetches."""

    def __init__(self, connection: psycopg.Connection[Any],
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.connection, self.now = connection, now
        self.tagger = MetadataTagger(PostgresAliasIndex(connection))

    def process(self, candidate: Mapping[str, object]) -> MetadataResult:
        raw_url, raw_title = candidate.get("url"), candidate.get("title")
        if not isinstance(raw_url, str) or not isinstance(raw_title, str):
            raise IngestInputError("candidate needs a url and a title")
        hint = candidate.get("source_hint")
        if not isinstance(hint, str) or not hint:
            raise IngestInputError("candidate needs a source_hint")
        key = url_hash(raw_url)
        title = clean_text(raw_title, 1000)
        if not title:
            raise IngestInputError("candidate title is empty")
        snippet = clean_text(candidate.get("snippet"))
        published_at = _published_at(candidate.get("published_at"))

        with self.connection.transaction():
            source = resolve_source(self.connection, hint)
            existing = self.connection.execute(
                "SELECT 1 FROM articles WHERE url_hash = %s", (key,)
            ).fetchone()
            if existing:
                return MetadataResult(None, None, True, None, source.id, source.tier_value, ())
            tagging = self.tagger.tag(title, snippet, source.domain)
            tier = tagging.tier
            row = self.connection.execute(
                """INSERT INTO articles (public_id, url_hash, url, source_id, title, snippet,
                                         published_at, tier)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (url_hash) DO NOTHING RETURNING id, public_id""",
                ("art_" + uuid4().hex, key, raw_url.strip(), source.id, title, snippet,
                 published_at, tier),
            ).fetchone()
            if row is None:  # a concurrent worker inserted the same URL first
                return MetadataResult(None, None, True, None, source.id, source.tier_value, ())
            article_id, public_id = row
            for entity in tagging.entities:
                self.connection.execute(
                    """INSERT INTO article_entities (article_id, entity_id, mentions, salience,
                                                     found_in)
                       VALUES (%s, %s, %s, %s, 'metadata')""",
                    (article_id, entity.entity_id, entity.mentions, entity.salience),
                )
            for ambiguity in tagging.ambiguous:
                self.connection.execute(
                    """INSERT INTO resolution_misses (surface_form, context, article_id)
                       VALUES (%s, %s, %s)""",
                    (ambiguity.surface,
                     json.dumps({"candidates": list(ambiguity.candidates), "stage": "metadata"}),
                     article_id),
                )
            if tier == 2:
                from app.quarantine import quarantine_article

                quarantine_article(self.connection, article_id,
                                   tuple(entity.entity_id for entity in tagging.entities),
                                   self.now())
        logger.info(
            "article tiered",
            extra={"article_public_id": public_id, "tier": tier, "source_id": source.id,
                   "entity_count": len(tagging.entities)},
        )
        return MetadataResult(article_id, public_id, False, tier, source.id, source.tier_value,
                              tuple(entity.entity_id for entity in tagging.entities), key)
