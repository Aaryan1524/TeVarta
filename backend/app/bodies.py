"""T21 body stage: conditional fetch, private storage, SimHash dedupe, body tags, embeddings.

§7.2 change B: only tier-1 articles have bodies fetched; tier 2 embeds title and
snippet; tier 3 gets nothing. Bodies are stored privately for processing and never
shown (§7.4). Fetches respect robots.txt. Paid calls are metered in usage_records
with the behaviour they measure.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from uuid import UUID, uuid4

import psycopg

from app.ingest import MetadataTagger, PostgresAliasIndex
from app.worker import celery_app

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 3_000_000
MAX_REDIRECTS = 3
MAX_STORED_CHARS = 200_000
MAX_TAGGED_CHARS = 20_000
MAX_EMBEDDED_CHARS = 24_000  # ~6k tokens, inside the model's 8,191-token input
EMBEDDING_DIMENSIONS = 1024  # frozen VECTOR(1024); decisions.md 2026-09-12
SIMHASH_MAX_DISTANCE = 12  # measured: near-duplicates <= 10 bits, unrelated >= 21 (decisions.md)
SIMHASH_WINDOW = timedelta(days=7)
# Less text than this is a script-rendered shell ("this application supports most
# recent browsers"), not an article: identical across pages, so never stored or compared.
MIN_BODY_CHARS = 300
# Only a specific live subject in a body promotes a primary-source article; countries,
# industries and commodities appear in nearly every filing's boilerplate.
PROMOTING_TYPES = ("Company", "Security", "Facility", "Product")
# ...and only when the body is about it: attendee lists and passing references name a
# company once or twice, while a company's own release or filing names it repeatedly.
PROMOTION_MIN_MENTIONS = 3
# A body near-duplicate is a copy of the same story only if the headlines agree too:
# blocked or templated pages are near-identical across unrelated documents.
TITLE_MATCH_MIN = 0.5
_TITLE_PREFIX = re.compile(r"^(update|refile|corrected|correction)\s*\d*\s*[-:]\s*")
_TITLE_STOPWORDS = frozenset({"the", "a", "an", "of", "to", "in", "on", "for", "and", "at"})


def title_similarity(left: str, right: str) -> float:
    """Token Jaccard of two headlines, ignoring wire prefixes such as "UPDATE 1-"."""
    def tokens(title: str) -> set[str]:
        title = _TITLE_PREFIX.sub("", title.casefold().strip())
        return set(re.findall(r"[a-z0-9]+", title)) - _TITLE_STOPWORDS
    a, b = tokens(left), tokens(right)
    return len(a & b) / len(a | b) if a | b else 0.0


class BodyFetchRefused(RuntimeError):
    """robots.txt or the response type does not allow processing this URL."""


class EmbeddingNotConfigured(RuntimeError):
    pass


# --- Fetching under robots.txt --------------------------------------------------------


class HttpGet(Protocol):
    def __call__(self, url: str, headers: Mapping[str, str]) -> tuple[int, str, bytes]: ...


class Redirect(Exception):
    """A 3xx response; the fetcher follows it hop by hop so every hop is vetted."""

    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise Redirect(newurl)


def http_get(url: str, headers: Mapping[str, str]) -> tuple[int, str, bytes]:
    """Status, content type, and at most MAX_BODY_BYTES of the response.

    Redirects are not followed here: a 3xx raises Redirect so the caller can re-check
    robots.txt and the destination address on every hop.
    """
    from app.discovery import tls_context

    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=tls_context()))
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with opener.open(request, timeout=20) as response:  # nosec B310
            return (response.status, response.headers.get("Content-Type", ""),
                    response.read(MAX_BODY_BYTES))
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", "") if error.headers else "", b""


def is_public_url(url: str) -> bool:
    """https/http to a host whose every address is public: blocks SSRF to private,
    loopback, link-local (cloud metadata) and reserved ranges."""
    import ipaddress
    import socket

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443)}
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


class RobotsPolicy:
    """Per-host robots.txt, cached for a day. 401/403 disallow; other 4xx allow."""

    def __init__(self, get: HttpGet, user_agent: str,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 allow_url: Callable[[str], bool] | None = None) -> None:
        self.get, self.user_agent, self.now = get, user_agent, now
        self.allow_url = allow_url
        self._cache: dict[str, tuple[datetime, RobotFileParser]] = {}

    def _fetch_robots(self, url: str) -> tuple[int, bytes]:
        """robots.txt often redirects (http→https, apex→www); follow a few hops. A hop
        that is not public http(s), or a redirect loop, means "unknown": disallow."""
        for _ in range(MAX_REDIRECTS + 1):
            try:
                status, _, content = self.get(url, {"User-Agent": self.user_agent})
            except Redirect as redirect:
                url = urljoin(url, redirect.location)
                if self.allow_url is not None and not self.allow_url(url):
                    return 503, b""
                continue
            return status, content
        return 503, b""

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._cache.get(origin)
        if cached is None or self.now() - cached[0] > timedelta(days=1):
            parser = RobotFileParser()
            status, content = self._fetch_robots(f"{origin}/robots.txt")
            if status in (401, 403):
                parser.disallow_all = True
            elif status >= 500:
                parser.disallow_all = True  # unknown: do not fetch
            elif status >= 400:
                parser.allow_all = True
            else:
                parser.parse(content.decode("utf-8", "replace").splitlines())
            cached = (self.now(), parser)
            self._cache[origin] = cached
        return cached[1].can_fetch(self.user_agent, url)


_DROP = re.compile(r"(?is)<(head|script|style|noscript|nav|header|footer|aside|form|svg)\b.*?</\1>")


def extract_text(content: bytes, content_type: str) -> str:
    """Readable text from an HTML or plain-text document."""
    charset = re.search(r"charset=([\w-]+)", content_type, flags=re.IGNORECASE)
    text = content.decode(charset.group(1) if charset else "utf-8", "replace")
    kind = content_type.lower()
    if "html" in kind or "xml" in kind or text.lstrip()[:1].startswith("<"):
        text = _DROP.sub(" ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)[:MAX_STORED_CHARS]


# EDGAR feed links point at a filing's index page, a table of documents. The filing's
# substance is the primary form document plus its press-release exhibits (EX-99.x,
# where foreign issuers' 6-Ks carry the news).
_SEC_INDEX = re.compile(
    r"^https://(www\.)?sec\.gov/Archives/edgar/data/\d+/\d+/[\w-]+-index\.html?$",
    flags=re.IGNORECASE,
)
_SEC_PRIMARY = re.compile(r"^(8-K|6-K|10-K|10-Q|20-F|40-F)(/A)?$")
_SEC_PRESS_EXHIBIT = re.compile(r"^EX-99(\.\d+)?$")
MAX_SEC_DOCUMENTS = 3


def sec_filing_documents(index_html: str) -> list[str]:
    """The primary form document first, then press-release exhibits; at most three."""
    primary, exhibits = [], []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", index_html):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
        if len(cells) < 4:
            continue
        link = re.search(r'href="([^"]+)"', cells[2])
        kind = html.unescape(re.sub(r"<[^>]+>", "", cells[3])).strip()
        if not link:
            continue
        href = link.group(1).removeprefix("/ix?doc=")
        if not href.startswith("/Archives/") or not href.lower().endswith((".htm", ".html")):
            continue
        if _SEC_PRIMARY.match(kind):
            primary.append(f"https://www.sec.gov{href}")
        elif _SEC_PRESS_EXHIBIT.match(kind):
            exhibits.append(f"https://www.sec.gov{href}")
    return (primary + exhibits)[:MAX_SEC_DOCUMENTS]


# FederalRegister.gov blocks automated HTML requests ("programmatic access ... is limited
# to ... developer APIs"); the document text is published at a predictable full-text path.
_FEDERAL_REGISTER_DOCUMENT = re.compile(
    r"^https://www\.federalregister\.gov/documents/(\d{4})/(\d{2})/(\d{2})/([0-9A-Za-z-]+)(/|$)"
)


def federal_register_text_url(url: str) -> str | None:
    match = _FEDERAL_REGISTER_DOCUMENT.match(url)
    if not match:
        return None
    year, month, day, number = match.group(1, 2, 3, 4)
    return (f"https://www.federalregister.gov/documents/full_text/text/"
            f"{year}/{month}/{day}/{number}.txt")



class BodyFetcher:
    def __init__(self, get: HttpGet, robots: RobotsPolicy, user_agent: str,
                 allow_url: Callable[[str], bool] | None = None) -> None:
        self.get, self.robots, self.user_agent = get, robots, user_agent
        self.allow_url = allow_url

    def fetch(self, url: str) -> str:
        url = federal_register_text_url(url) or url
        content_type, content = self._get(url)
        if _SEC_INDEX.match(url):
            documents = sec_filing_documents(content.decode("utf-8", "replace"))
            texts = []
            for document in documents:
                try:
                    document_type, document_content = self._get(document)
                except BodyFetchRefused:
                    continue
                texts.append(extract_text(document_content, document_type))
            text = "\n\n".join(part for part in texts if part)[:MAX_STORED_CHARS]
            if len(text) >= MIN_BODY_CHARS:
                return text
        text = extract_text(content, content_type)
        if len(text) < MIN_BODY_CHARS:
            raise BodyFetchRefused("no usable text (empty or script-rendered page)")
        return text

    def _get(self, url: str) -> tuple[str, bytes]:
        for _ in range(MAX_REDIRECTS + 1):
            if self.allow_url is not None and not self.allow_url(url):
                raise BodyFetchRefused("destination is not a public http(s) address")
            if not self.robots.allowed(url):
                raise BodyFetchRefused("robots.txt disallows this URL")
            try:
                status, content_type, content = self.get(
                    url, {"User-Agent": self.user_agent, "Accept": "text/html, text/plain"})
            except Redirect as redirect:
                url = urljoin(url, redirect.location)
                continue
            break
        else:
            raise BodyFetchRefused("too many redirects")
        if status != 200:
            raise BodyFetchRefused(f"HTTP {status}")
        kind = content_type.lower()
        if kind and not any(t in kind for t in ("html", "text/plain", "xml")):
            raise BodyFetchRefused("not a text document")
        return content_type, content


# --- SimHash ------------------------------------------------------------------------


def simhash(text: str) -> int:
    """64-bit SimHash over word 3-shingles, as a signed integer for Postgres BIGINT."""
    words = re.findall(r"[a-z0-9]+", text.casefold())
    shingles = [" ".join(words[i:i + 3]) for i in range(max(1, len(words) - 2))]
    counts = [0] * 64
    for shingle in shingles:
        value = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            counts[bit] += 1 if value >> bit & 1 else -1
    unsigned = sum(1 << bit for bit in range(64) if counts[bit] > 0)
    return unsigned - (1 << 64) if unsigned >= 1 << 63 else unsigned


def hamming(left: int, right: int) -> int:
    return bin((left ^ right) & ((1 << 64) - 1)).count("1")


# --- Private storage ----------------------------------------------------------------


class BodyStore(Protocol):
    def put(self, text: str) -> tuple[str, int]: ...


class LocalBodyStore:
    """Content-addressed private storage for development; R2 implements the same put."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, text: str) -> tuple[str, int]:
        data = text.encode()
        key = f"bodies/{hashlib.sha256(data).hexdigest()}.txt"
        path = self.root / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return key, len(data)


# --- Embeddings ---------------------------------------------------------------------


@dataclass(frozen=True)
class Embedding:
    vector: tuple[float, ...]
    input_tokens: int


class Embedder(Protocol):
    provider: str
    model: str
    usd_per_mtok: float

    def embed(self, text: str) -> Embedding: ...


class OpenAIEmbedder:
    URL = "https://api.openai.com/v1/embeddings"
    provider = "openai"

    def __init__(self, api_key: str, model: str, usd_per_mtok: float,
                 post: Callable[[str, bytes, Mapping[str, str]], bytes] | None = None) -> None:
        if not api_key.strip():
            raise EmbeddingNotConfigured("OPENAI_API_KEY is empty")
        self._api_key, self.model, self.usd_per_mtok = api_key.strip(), model, usd_per_mtok
        self._post = post or _post_json

    def embed(self, text: str) -> Embedding:
        body = json.dumps({"model": self.model, "input": text,
                           "dimensions": EMBEDDING_DIMENSIONS}).encode()
        payload = json.loads(self._post(self.URL, body, {
            "Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}))
        vector = tuple(float(v) for v in payload["data"][0]["embedding"])
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"embedding has {len(vector)} dimensions, expected 1024")
        return Embedding(vector, int(payload.get("usage", {}).get("prompt_tokens", 0)))


def _post_json(url: str, body: bytes, headers: Mapping[str, str]) -> bytes:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 -- fixed URL
        return response.read()


def embedder_from_env(environ: Mapping[str, str] | None = None) -> OpenAIEmbedder:
    """Fail closed without a key or a price: unmetered spend is not allowed."""
    values = os.environ if environ is None else environ
    price = values.get("TEVARTA_EMBEDDING_USD_PER_MTOK", "").strip()
    if not price:
        raise EmbeddingNotConfigured("set TEVARTA_EMBEDDING_USD_PER_MTOK to meter embeddings")
    return OpenAIEmbedder(values.get("OPENAI_API_KEY", ""),
                          values.get("TEVARTA_EMBEDDING_MODEL", "text-embedding-3-small"),
                          float(price))


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


# --- The stage ----------------------------------------------------------------------


MODES = ("default", "override", "audit")


@dataclass(frozen=True)
class BodyResult:
    article_id: str
    tier: int  # the tier after this stage (an override may promote to 1)
    fetched: str  # 'stored' | 'duplicate' | 'refused' | 'failed' | 'not_eligible'
    embedding: str  # 'body' | 'title_snippet' | 'copied' | 'none' | 'not_configured' | 'failed'
    body_entities_added: int
    promoted: bool = False
    audited: bool = False

    def as_payload(self) -> dict[str, object]:
        return self.__dict__.copy()


class BodyStage:
    def __init__(self, connection: psycopg.Connection[Any], fetcher: BodyFetcher | None,
                 store: BodyStore, embedder: Embedder | None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.connection, self.fetcher, self.store = connection, fetcher, store
        self.embedder, self.now = embedder, now
        self.tagger = MetadataTagger(PostgresAliasIndex(connection))

    def process(self, article_id: UUID | str, *, mode: str = "default",
                sampled: bool = False) -> BodyResult:
        """Apply the tier's treatment.

        default  — §7.2: tier 1 body, tier 2 title+snippet embedding, tier 3 nothing.
        override — T22 high-tier source: always fetch; a live entity in the body promotes
                   the article to tier 1 so primary sources can reach the fast path.
        audit    — T22 5% tier-3 sample: fetch and record what the body reveals in
                   recall_audit; never promoted or embedded, so the measurement is pure.
        sampled  — the article is in the 5% tier-3 sample but its source is a primary
                   one (override). Its original tier-3 assignment is audited before any
                   promotion; otherwise §17 would get no samples when every source is
                   tier_value >= 0.9.
        """
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        row = self.connection.execute(
            """SELECT a.url, a.title, a.snippet, a.tier, a.published_at, s.domain
               FROM articles a JOIN sources s ON s.id = a.source_id WHERE a.id = %s""",
            (article_id,),
        ).fetchone()
        self.connection.commit()
        if row is None:
            raise LookupError("article not found")
        url, title, snippet, tier, published_at, domain = row
        if mode == "default" and tier != 1:
            return BodyResult(str(article_id), tier, "not_eligible",
                              self._embed_for_tier(article_id, tier, title, snippet, None), 0)
        if self.fetcher is None:
            return self._without_body(article_id, tier, mode, title, snippet, "refused")
        try:
            text = self.fetcher.fetch(url)
        except BodyFetchRefused as refusal:
            logger.info("body not fetched", extra={"article_id": str(article_id),
                                                   "reason": str(refusal)})
            return self._without_body(article_id, tier, mode, title, snippet, "refused")
        except Exception:
            logger.warning("body fetch failed", extra={"article_id": str(article_id)})
            return self._without_body(article_id, tier, mode, title, snippet, "failed")

        fingerprint = simhash(text)
        original = self._near_duplicate(article_id, fingerprint, published_at, title)
        found, added = self._tag_body(article_id, text, domain)
        audited = promoted = False
        if mode == "audit" or (sampled and tier == 3):
            audited = self._record_audit(article_id, tier, found)
        if mode == "override" and tier != 1 and self._names_live_subject(found):
            self._promote(article_id)
            tier, promoted = 1, True

        if original is not None:
            # Same story syndicated elsewhere: keep this article (its publisher is
            # provenance) but reuse the stored body and embedding instead of paying again.
            r2_key, embedding, scope = original
            copy = embedding is not None and scope == "body" and tier == 1
            with self.connection.transaction():
                self.connection.execute(
                    """UPDATE articles SET fetched_at = %s, simhash = %s, r2_key = %s,
                              embedding = %s::vector, embedding_scope = %s WHERE id = %s""",
                    (self.now(), fingerprint, r2_key, embedding if copy else None,
                     scope if copy else None, article_id),
                )
            embedded = "copied" if copy else (
                "none" if tier != 2 or audited
                else self._embed_for_tier(article_id, tier, title, snippet, None))
            return BodyResult(str(article_id), tier, "duplicate", embedded, added,
                              promoted, audited)

        key, size = self.store.put(text)
        with self.connection.transaction():
            self.connection.execute(
                "UPDATE articles SET fetched_at = %s, simhash = %s, r2_key = %s WHERE id = %s",
                (self.now(), fingerprint, key, article_id),
            )
            self._meter("r2", "success", size, "byte", 0.0)
        embedded = "none" if audited else self._embed_for_tier(
            article_id, tier, title, snippet, text)
        return BodyResult(str(article_id), tier, "stored", embedded, added, promoted, audited)

    def _without_body(self, article_id: UUID | str, tier: int, mode: str, title: str,
                      snippet: str | None, fetched: str) -> BodyResult:
        """No usable body: tier 1 and 2 still get a title+snippet embedding so they
        stay clusterable; the audit sample is never embedded."""
        embedded = "none"
        if mode != "audit" and tier in (1, 2):
            embedded = self._embed(article_id, f"{title}\n{snippet or ''}", "title_snippet")
        return BodyResult(str(article_id), tier, fetched, embedded, 0)

    def _names_live_subject(self, found: Sequence[Any]) -> bool:
        live = [entity.entity_id for entity in found
                if entity.status == "live" and entity.mentions >= PROMOTION_MIN_MENTIONS]
        if not live:
            return False
        row = self.connection.execute(
            "SELECT 1 FROM entities WHERE entity_id = ANY(%s) AND type = ANY(%s) LIMIT 1",
            (live, list(PROMOTING_TYPES)),
        ).fetchone()
        self.connection.commit()
        return row is not None

    def _embed_for_tier(self, article_id: UUID | str, tier: int, title: str,
                        snippet: str | None, body: str | None) -> str:
        """The §7.2 embedding table: tier 1 body, tier 2 title+snippet, tier 3 none."""
        if tier == 1 and body is not None:
            return self._embed(article_id, f"{title}\n{body}", "body")
        if tier == 2:
            return self._embed(article_id, f"{title}\n{snippet or ''}", "title_snippet")
        return "none"

    def _promote(self, article_id: UUID | str) -> None:
        with self.connection.transaction():
            self.connection.execute("UPDATE articles SET tier = 1 WHERE id = %s", (article_id,))
            self.connection.execute("DELETE FROM quarantine WHERE article_id = %s", (article_id,))

    def _record_audit(self, article_id: UUID | str, tier: int,
                      found: Sequence[Any]) -> bool:
        with self.connection.transaction():
            return self.connection.execute(
                """INSERT INTO recall_audit (article_id, tier_assigned, entities_found_in_body)
                   VALUES (%s, %s, %s) ON CONFLICT (article_id) DO NOTHING""",
                (article_id, tier, sorted(entity.entity_id for entity in found)),
            ).rowcount == 1

    def _near_duplicate(self, article_id: UUID | str, fingerprint: int,
                        published_at: datetime, title: str) -> tuple[str, Any, str | None] | None:
        rows = self.connection.execute(
            """SELECT r2_key, embedding, embedding_scope, title FROM articles
               WHERE id <> %s AND simhash IS NOT NULL AND r2_key IS NOT NULL
                 AND published_at BETWEEN %s AND %s
                 AND bit_count((simhash # %s)::bit(64)) <= %s
               ORDER BY bit_count((simhash # %s)::bit(64)), fetched_at LIMIT 20""",
            (article_id, published_at - SIMHASH_WINDOW, published_at + SIMHASH_WINDOW,
             fingerprint, SIMHASH_MAX_DISTANCE, fingerprint),
        ).fetchall()
        self.connection.commit()
        for r2_key, embedding, scope, other_title in rows:
            if title_similarity(title, other_title) >= TITLE_MATCH_MIN:
                return r2_key, embedding, scope
        return None

    def _tag_body(self, article_id: UUID | str, text: str,
                  domain: str) -> tuple[tuple[Any, ...], int]:
        """Body tags may add entities (§7.2). Returns every entity found and the count new."""
        tagging = self.tagger.tag(text[:MAX_TAGGED_CHARS], None, domain)
        added = 0
        with self.connection.transaction():
            for entity in tagging.entities:
                added += self.connection.execute(
                    """INSERT INTO article_entities (article_id, entity_id, mentions, salience,
                                                     found_in)
                       VALUES (%s, %s, %s, 0.6, 'body') ON CONFLICT DO NOTHING""",
                    (article_id, entity.entity_id, entity.mentions),
                ).rowcount
            for ambiguity in tagging.ambiguous:
                self.connection.execute(
                    """INSERT INTO resolution_misses (surface_form, context, article_id)
                       VALUES (%s, %s, %s)""",
                    (ambiguity.surface,
                     json.dumps({"candidates": list(ambiguity.candidates), "stage": "body"}),
                     article_id),
                )
        return tagging.entities, added

    def _embed(self, article_id: UUID | str, text: str, scope: str) -> str:
        if self.embedder is None:
            return "not_configured"
        started = monotonic()
        try:
            result = self.embedder.embed(text[:MAX_EMBEDDED_CHARS])
        except Exception:
            logger.warning("embedding failed", extra={"article_id": str(article_id)})
            with self.connection.transaction():
                self._meter("embeddings", "failure", 0, "token", 0.0,
                            latency_ms=int((monotonic() - started) * 1000))
            return "failed"
        cost = result.input_tokens * self.embedder.usd_per_mtok / 1_000_000
        with self.connection.transaction():
            self.connection.execute(
                "UPDATE articles SET embedding = %s::vector, embedding_scope = %s WHERE id = %s",
                (vector_literal(result.vector), scope, article_id),
            )
            self._meter("embeddings", "success", result.input_tokens, "token", cost,
                        input_tokens=result.input_tokens,
                        latency_ms=int((monotonic() - started) * 1000))
        return scope

    def _meter(self, component: str, outcome: str, quantity: int, unit: str, cost: float,
               **extra: int) -> None:
        provider = (self.embedder.provider if component == "embeddings" and self.embedder
                    else "local" if component == "r2" else None)
        model = self.embedder.model if component == "embeddings" and self.embedder else None
        self.connection.execute(
            """INSERT INTO usage_records (request_id, component, outcome, quantity, unit,
                                          estimated_cost_usd, provider, model, input_tokens,
                                          latency_ms)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (uuid4(), component, outcome, quantity, unit, round(cost, 8), provider, model,
             extra.get("input_tokens"), extra.get("latency_ms")),
        )


# --- Worker wiring ----------------------------------------------------------------


def body_stage_from_env(connection: psycopg.Connection[Any]) -> BodyStage:
    """Fetch only with a declared contact and a configured private store."""
    from app.discovery import DiscoveryConfigurationError, declared_user_agent

    fetcher = None
    store_dir = os.getenv("TEVARTA_BODY_STORE_DIR", "").strip()
    try:
        user_agent = declared_user_agent()
        if store_dir:
            robots = RobotsPolicy(http_get, user_agent, allow_url=is_public_url)
            fetcher = BodyFetcher(http_get, robots, user_agent, allow_url=is_public_url)
    except DiscoveryConfigurationError:
        fetcher = None
    try:
        embedder: Embedder | None = embedder_from_env()
    except EmbeddingNotConfigured:
        embedder = None
    store = LocalBodyStore(Path(store_dir or "/nonexistent"))
    return BodyStage(connection, fetcher, store, embedder)


@celery_app.task(name="app.bodies.process_article")
def process_article(article_id: str, mode: str = "default",
                    sampled: bool = False) -> dict[str, object]:
    with psycopg.connect(os.environ["TEVARTA_POSTGRES_URL"]) as connection:
        stage = body_stage_from_env(connection)
        return stage.process(article_id, mode=mode, sampled=sampled).as_payload()
