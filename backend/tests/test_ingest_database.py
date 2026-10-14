"""T20 metadata stage against a migrated PostgreSQL database."""

import http.client
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from app.discovery import SourceConfig, load_sources
from app.ingest import (
    WEB_SOURCE_TIER,
    IngestInputError,
    MetadataStage,
    resolve_source,
    sync_sources,
)

pytestmark = pytest.mark.database

FEED = SourceConfig("t20_feed", "example.org", "https://example.org/feed", "t20_group",
                    0.7, 0.9, 300, False, "rss")


@pytest.fixture
def conn(migrated_dsn):
    """One rolled-back transaction per test; MetadataStage nests a savepoint inside it."""
    with psycopg.connect(migrated_dsn) as connection:
        sync_sources(connection, [FEED])
        connection.execute(
            """INSERT INTO entities (entity_id, canonical_name, type, status) VALUES
               ('ent_t20_taiwan', 'Taiwan', 'Country', 'live'),
               ('ent_t20_sandisk', 'SanDisk Corp', 'Company', 'catalog'),
               ('ent_t20_mercury_a', 'Mercury A', 'Company', 'catalog'),
               ('ent_t20_mercury_b', 'Mercury B', 'Company', 'catalog')"""
        )
        connection.execute(
            """INSERT INTO entity_aliases VALUES
               ('taiwan', 'ent_t20_taiwan', 'test', 1, false),
               ('sandisk', 'ent_t20_sandisk', 'test', 1, false),
               ('mercury', 'ent_t20_mercury_a', 'test', 1, false),
               ('mercury', 'ent_t20_mercury_b', 'test', 1, false)"""
        )
        yield connection
        connection.rollback()


def candidate(title, url=None, snippet=None, source_hint="t20_feed"):
    return {
        "url": url or f"https://example.org/{uuid4().hex}",
        "title": title,
        "snippet": snippet,
        "publisher": "t20_group",
        "published_at": "2026-10-13T08:00:00Z",
        "source_hint": source_hint,
    }


def article(conn, public_id):
    return conn.execute(
        "SELECT tier, source_id, fetched_at, embedding_scope FROM articles WHERE public_id = %s",
        (public_id,),
    ).fetchone()


def test_tiers_follow_live_and_catalog_status(conn):
    stage = MetadataStage(conn)
    live = stage.process(candidate("Major earthquake strikes eastern Taiwan"))
    catalog = stage.process(candidate("SanDisk expands flash output"))
    unmatched = stage.process(candidate("Markets drift before the holiday"))

    assert (live.tier, catalog.tier, unmatched.tier) == (1, 2, 3)
    assert live.entity_ids == ("ent_t20_taiwan",)
    assert conn.execute(
        """SELECT entity_id, mentions, salience, found_in FROM article_entities
           WHERE article_id = %s""", (live.article_id,),
    ).fetchall() == [("ent_t20_taiwan", 1, 1, "metadata")]
    # Metadata only: nothing fetched or embedded at this stage.
    assert article(conn, live.public_id) == (1, "t20_feed", None, None)


def test_equivalent_urls_are_processed_once(conn):
    stage = MetadataStage(conn)
    first = stage.process(candidate("Taiwan exports rise", "https://example.org/n/1?utm_source=a"))
    second = stage.process(candidate("Taiwan exports rise", "http://www.example.org/n/1/"))

    assert not first.duplicate and second.duplicate and second.article_id is None
    assert conn.execute(
        "SELECT count(*) FROM articles WHERE url LIKE 'https://example.org/n/1%%'"
    ).fetchone()[0] == 1


def test_ambiguous_alias_tags_nothing_and_logs_a_miss(conn):
    result = MetadataStage(conn).process(candidate("Mercury rising"))

    assert result.tier == 3 and result.entity_ids == ()
    surface, context = conn.execute(
        "SELECT surface_form, context FROM resolution_misses WHERE article_id = %s",
        (result.article_id,),
    ).fetchone()
    assert surface == "Mercury"
    assert '"stage": "metadata"' in context and "ent_t20_mercury_b" in context


def test_no_network_request_happens_before_tiering(conn, monkeypatch):
    def forbidden(*_, **__):
        raise AssertionError("the metadata stage must not make network requests")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(http.client.HTTPConnection, "request", forbidden)

    assert MetadataStage(conn).process(candidate("Taiwan rains")).tier == 1


def test_high_tier_source_does_not_change_the_metadata_tier(conn):
    # The always-fetch override (§7.2) is a T22 fetch decision, not a tier rewrite.
    sync_sources(conn, [SourceConfig("t20_regulator", "regulator.example.org", None,
                                     "t20_reg", 1.0, 0.99, 300, False, "rss")])
    result = MetadataStage(conn).process(
        candidate("Agency publishes routine notice", source_hint="t20_regulator")
    )
    assert (result.tier, result.tier_value) == (3, 1.0)


def test_search_hosts_map_to_configured_publishers_or_enter_as_unvetted(conn):
    assert resolve_source(conn, "web:news.example.org").id == "t20_feed"
    unvetted = resolve_source(conn, "web:www.Unknown-Blog.net")
    assert (unvetted.id, unvetted.publisher_group, unvetted.tier_value) == (
        "web:unknown-blog.net", "unknown-blog.net", WEB_SOURCE_TIER
    )
    with pytest.raises(IngestInputError, match="unknown source"):
        resolve_source(conn, "not_configured")


def test_source_sync_mirrors_the_registry_idempotently(conn):
    sources = load_sources()
    assert sync_sources(conn, sources) == len(sources)
    assert sync_sources(conn, sources) == len(sources)
    assert conn.execute(
        "SELECT publisher_group, tier_value, active FROM sources WHERE id = 'sec_current_filings'"
    ).fetchone() == ("us_sec", 1, False)


def test_concurrent_workers_insert_one_article(migrated_dsn):
    with psycopg.connect(migrated_dsn) as setup:
        sync_sources(setup, [FEED])
    url = f"https://example.org/race/{uuid4().hex}"

    def work(_):
        with psycopg.connect(migrated_dsn) as connection:
            return MetadataStage(connection).process(candidate("Quiet day", url)).duplicate

    with ThreadPoolExecutor(max_workers=8) as pool:
        duplicates = list(pool.map(work, range(16)))

    assert duplicates.count(False) == 1
    with psycopg.connect(migrated_dsn) as check:
        count = check.execute("SELECT count(*) FROM articles WHERE url = %s", (url,)).fetchone()
        assert count[0] == 1


SEC_ATOM = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
<title>8-K - T20 FILER HOLDINGS (0009990001) (Filer)</title>
<link rel="alternate" type="text/html"
 href="https://www.sec.gov/Archives/edgar/data/9990001/000999000126000001-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-10-13 &lt;b&gt;AccNo:&lt;/b&gt; 1</summary>
<updated>2026-10-13T16:05:12-04:00</updated>
</entry>
</feed>"""


def test_feed_entry_flows_from_poller_to_tier_without_a_body_fetch(conn):
    from app.discovery import FeedPoller

    sec = SourceConfig("t20_sec", "sec.gov", "https://www.sec.gov/feed", "us_sec",
                       1.0, 0.99, 300, True, "atom")
    sync_sources(conn, [sec])
    conn.execute(
        """INSERT INTO entities (entity_id, canonical_name, type, cik, status)
           VALUES ('ent_t20_filer', 'T20 Filer Holdings', 'Company', '0009990001', 'live')"""
    )
    conn.execute(
        "INSERT INTO entity_aliases VALUES ('cik:0009990001', 'ent_t20_filer', 'test', 1, true)"
    )
    poller = FeedPoller(fetcher=lambda url, headers: SEC_ATOM, user_agent="TeVarta/1.0 (t@t.io)")
    (entry,) = poller.poll(sec)

    result = MetadataStage(conn).process(entry.as_payload())

    assert (result.tier, result.entity_ids) == (1, ("ent_t20_filer",))
    stored = conn.execute(
        "SELECT title, snippet, fetched_at FROM articles WHERE id = %s", (result.article_id,)
    ).fetchone()
    assert stored == ("8-K - T20 FILER HOLDINGS (0009990001) (Filer)",
                      "Filed: 2026-10-13 AccNo: 1", None)


def test_celery_task_commits_the_tiered_article(migrated_dsn, monkeypatch):
    from app.discovery import process_candidate

    with psycopg.connect(migrated_dsn) as setup:
        sync_sources(setup, [FEED])
    monkeypatch.setenv("TEVARTA_POSTGRES_URL", migrated_dsn)
    entry = candidate("Calm markets")

    first = process_candidate.run(entry)
    replay = process_candidate.run(entry)

    assert (first["tier"], first["duplicate"], replay["duplicate"]) == (3, False, True)
    with psycopg.connect(migrated_dsn) as check:
        assert check.execute(
            "SELECT tier FROM articles WHERE public_id = %s", (first["public_id"],)
        ).fetchone() == (3,)
