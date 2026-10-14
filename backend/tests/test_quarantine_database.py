"""T22 quarantine, retention, override, and recall audit against PostgreSQL."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from app.bodies import EMBEDDING_DIMENSIONS, BodyStage, Embedding, LocalBodyStore
from app.discovery import SourceConfig
from app.ingest import MetadataStage, sync_sources, url_hash
from app.quarantine import audit_sampled, ingest_report, purge_expired
from conftest import alembic_config
from sqlalchemy import create_engine

pytestmark = pytest.mark.database

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
FEED = SourceConfig("t22_feed", "news.example.org", None, "t22_news", 0.7, 0.9, 300, False,
                    "rss")
REGULATOR = SourceConfig("t22_sec", "regulator.example.org", None, "t22_sec", 1.0, 0.99, 300,
                         False, "rss")


@pytest.fixture(scope="module")
def dsn(migrated_url):
    value = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(value) as connection:
        sync_sources(connection, [FEED, REGULATOR])
        connection.execute(
            """INSERT INTO entities (entity_id, canonical_name, type, status) VALUES
               ('ent_t22_taiwan', 'Taiwan', 'Country', 'live'),
               ('ent_t22_sandisk', 'SanDisk Corp', 'Company', 'catalog'),
               ('ent_t22_kioxia', 'Kioxia Holdings', 'Company', 'catalog'),
               ('ent_t22_tsmc', 'TSMC', 'Company', 'live')"""
        )
        connection.execute(
            """INSERT INTO entity_aliases VALUES
               ('taiwan', 'ent_t22_taiwan', 'test', 1, false),
               ('sandisk', 'ent_t22_sandisk', 'test', 1, false),
               ('kioxia', 'ent_t22_kioxia', 'test', 1, false),
               ('tsmc', 'ent_t22_tsmc', 'test', 1, false)"""
        )
    return value


def ingest(dsn, title, source="t22_feed", now=NOW, sampled=None):
    while True:
        url = f"https://news.example.org/{uuid4().hex}"
        if sampled is None or audit_sampled(url_hash(url)) is sampled:
            break
    with psycopg.connect(dsn) as connection:
        return MetadataStage(connection, now=lambda: now).process({
            "url": url, "title": title, "snippet": None, "publisher": "t22",
            "published_at": now.isoformat(), "source_hint": source})


def one(dsn, sql, *params):
    with psycopg.connect(dsn) as connection:
        return connection.execute(sql, params).fetchone()


def test_tier_two_is_quarantined_for_thirty_days_and_counted_as_demand(dsn):
    # Weeks no other test writes to, so the counts are exact in any test order.
    start = NOW + timedelta(days=700)
    first = ingest(dsn, "SanDisk and Kioxia expand flash output", now=start)
    ingest(dsn, "SanDisk raises prices", now=start)
    ingest(dsn, "SanDisk hires", now=start + timedelta(days=7))

    tagged, expires = one(dsn, """SELECT tagged_entity_ids, expires_at FROM quarantine
                                  WHERE article_id = %s""", first.article_id)
    assert (first.tier, sorted(tagged), expires) == (
        2, ["ent_t22_kioxia", "ent_t22_sandisk"], start + timedelta(days=30))
    week = start.date() - timedelta(days=start.weekday())
    with psycopg.connect(dsn) as connection:
        demand = connection.execute(
            """SELECT entity_id, week, quarantined_article_count FROM demand_log
               WHERE week >= %s ORDER BY week, entity_id""", (week,),
        ).fetchall()
    assert [(e, w.isoformat(), c) for e, w, c in demand] == [
        ("ent_t22_kioxia", week.isoformat(), 1), ("ent_t22_sandisk", week.isoformat(), 2),
        ("ent_t22_sandisk", (week + timedelta(days=7)).isoformat(), 1)]


def test_tier_one_and_three_are_never_quarantined(dsn):
    live = ingest(dsn, "Taiwan storm closes ports")
    unmatched = ingest(dsn, "Markets drift")
    for result in (live, unmatched):
        assert one(dsn, "SELECT count(*) FROM quarantine WHERE article_id = %s",
                   result.article_id) == (0,)


def test_retention_follows_ingestion_time_and_keeps_audited_articles(dsn):
    def aged(title, hours, tier_expected):
        result = ingest(dsn, title)
        assert result.tier == tier_expected
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE articles SET ingested_at = %s WHERE id = %s",
                (NOW - timedelta(hours=hours), result.article_id),
            )
        return result.article_id

    tier3_expired = aged("Old quiet day", 49, 3)
    tier3_fresh = aged("Recent quiet day", 47, 3)
    tier3_audited = aged("Audited quiet day", 72, 3)
    tier2_expired = aged("SanDisk old news", 31 * 24, 2)
    tier2_fresh = aged("SanDisk recent news", 29 * 24, 2)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO recall_audit (article_id, tier_assigned, entities_found_in_body) "
            "VALUES (%s, 3, '{}')", (tier3_audited,),
        )
        report = purge_expired(connection, NOW)
        remaining = {row[0] for row in connection.execute(
            "SELECT id FROM articles WHERE id = ANY(%s)",
            ([tier3_expired, tier3_fresh, tier3_audited, tier2_expired, tier2_fresh],),
        ).fetchall()}
    assert report.tier2 >= 1 and report.tier3 >= 1
    assert remaining == {tier3_fresh, tier3_audited, tier2_fresh}
    assert one(dsn, "SELECT count(*) FROM quarantine WHERE article_id = %s",
               tier2_expired) == (0,)


class Fetcher:
    def __init__(self, text):
        self.text = text

    def fetch(self, url):
        return self.text + " " + uuid4().hex * 40


class Embedder:
    provider, model, usd_per_mtok = "fake", "fake", 0.02

    def __init__(self):
        self.scopes = []

    def embed(self, text):
        return Embedding(tuple([0.02] * EMBEDDING_DIMENSIONS), 10)


def body_stage(dsn, tmp_path, text):
    connection = psycopg.connect(dsn)
    return connection, BodyStage(connection, Fetcher(text), LocalBodyStore(tmp_path), Embedder())


def test_a_primary_source_naming_a_live_entity_in_its_body_is_promoted(dsn, tmp_path):
    filing = ingest(dsn, "SanDisk files current report", source="t22_sec")
    assert filing.tier == 2
    connection, stage = body_stage(
        dsn, tmp_path, "TSMC said wafer supply was disrupted. TSMC expects delays. "
                       "Customers of TSMC were notified.")
    with connection:
        result = stage.process(filing.article_id, mode="override")
    assert (result.promoted, result.tier, result.embedding) == (True, 1, "body")
    assert one(dsn, "SELECT tier, embedding_scope FROM articles WHERE id = %s",
               filing.article_id) == (1, "body")
    assert one(dsn, "SELECT count(*) FROM quarantine WHERE article_id = %s",
               filing.article_id) == (0,)


def test_a_passing_mention_of_a_live_company_does_not_promote(dsn, tmp_path):
    # Attendee lists and asides name a company once; the subject of a filing recurs.
    filing = ingest(dsn, "SanDisk files committee minutes", source="t22_sec")
    connection, stage = body_stage(dsn, tmp_path, "Members present included TSMC and others.")
    with connection:
        result = stage.process(filing.article_id, mode="override")
    assert (result.promoted, result.tier) == (False, 2)


def test_a_live_country_only_in_the_body_does_not_promote(dsn, tmp_path):
    # Countries appear in nearly every filing's risk factors; only headlines promote them.
    filing = ingest(dsn, "SanDisk files quarterly notice", source="t22_sec")
    connection, stage = body_stage(dsn, tmp_path, "Operations in Taiwan continued normally.")
    with connection:
        result = stage.process(filing.article_id, mode="override")
    assert (result.promoted, result.tier) == (False, 2)


def test_a_primary_source_without_live_entities_keeps_its_tier(dsn, tmp_path):
    filing = ingest(dsn, "SanDisk files routine notice", source="t22_sec")
    connection, stage = body_stage(dsn, tmp_path, "Kioxia remained a joint venture partner.")
    with connection:
        result = stage.process(filing.article_id, mode="override")
    assert (result.promoted, result.tier, result.fetched, result.embedding) == (
        False, 2, "stored", "title_snippet")


def test_the_audit_records_body_findings_without_promoting(dsn, tmp_path):
    sampled = ingest(dsn, "A regional update")
    assert sampled.tier == 3
    connection, stage = body_stage(dsn, tmp_path, "Paragraph six notes SanDisk plants in Taiwan.")
    with connection:
        result = stage.process(sampled.article_id, mode="audit")
    assert (result.audited, result.promoted, result.tier, result.embedding) == (
        True, False, 3, "none")
    assert one(dsn, """SELECT tier_assigned, entities_found_in_body FROM recall_audit
                       WHERE article_id = %s""", sampled.article_id) == (
        3, ["ent_t22_sandisk", "ent_t22_taiwan"])
    assert one(dsn, "SELECT tier FROM articles WHERE id = %s", sampled.article_id) == (3,)
    with psycopg.connect(dsn) as connection:
        audit = ingest_report(connection, NOW - timedelta(days=30))["tier3_audit"]
    assert audit["audited"] >= 1 and audit["with_live_entities"] >= 1


def test_routing_sends_primary_sources_and_the_sample_to_the_body_stage(dsn, monkeypatch):
    from app import bodies
    from app.discovery import process_candidate

    queued = []
    monkeypatch.setattr(bodies.process_article, "delay", lambda *args: queued.append(args))
    monkeypatch.setenv("TEVARTA_POSTGRES_URL", dsn)

    def run(title, source, sampled):
        while True:
            url = f"https://news.example.org/{uuid4().hex}"
            if audit_sampled(url_hash(url)) is sampled:
                break
        return process_candidate.run({
            "url": url, "title": title, "snippet": None, "publisher": "t22",
            "published_at": NOW.isoformat(), "source_hint": source})

    primary = run("Agency notice", "t22_sec", False)
    sampled = run("Quiet sampled day", "t22_feed", True)
    skipped = run("Quiet unsampled day", "t22_feed", False)
    assert queued == [(primary["article_id"], "override"), (sampled["article_id"], "audit")]
    assert skipped["body_mode"] is None


def test_ingested_at_migration_roundtrip(database_factory):
    url = database_factory()
    engine = create_engine(url)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
        command.downgrade(alembic_config(connection), "0005_expansion_lifecycle")
        command.upgrade(alembic_config(connection), "head")
    engine.dispose()


def test_a_purged_article_is_not_reingested_while_its_feed_still_lists_it(dsn):
    url = f"https://news.example.org/{uuid4().hex}"
    entry = {"url": url, "title": "Quiet regional update", "snippet": None, "publisher": "t22",
             "published_at": NOW.isoformat(), "source_hint": "t22_feed"}

    def process(now):
        with psycopg.connect(dsn) as connection:
            return MetadataStage(connection, now=lambda: now).process(dict(entry))

    first = process(NOW)
    assert not first.duplicate and first.tier == 3
    later = NOW + timedelta(hours=49)
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE articles SET ingested_at = %s WHERE id = %s",
                           (NOW - timedelta(hours=1) - timedelta(hours=48), first.article_id))
        assert purge_expired(connection, later).tier3 >= 1
    assert one(dsn, "SELECT count(*) FROM articles WHERE url = %s", url) == (0,)
    again = process(later)  # the next poll: the feed still lists the item
    assert again.duplicate and again.article_id is None
    assert one(dsn, "SELECT count(*) FROM articles WHERE url = %s", url) == (0,)
    # The tombstone expires on its own clock, so the table cannot grow without bound.
    with psycopg.connect(dsn) as connection:
        assert purge_expired(connection, NOW + timedelta(days=31)).tombstones >= 1
    assert process(NOW + timedelta(days=31)).duplicate is False
