"""T21 body stage against a migrated PostgreSQL database."""

from uuid import uuid4

import psycopg
import pytest
from app.bodies import EMBEDDING_DIMENSIONS, BodyFetchRefused, BodyStage, Embedding, LocalBodyStore
from app.discovery import SourceConfig
from app.ingest import MetadataStage, sync_sources

pytestmark = pytest.mark.database

FEED = SourceConfig("t21_feed", "news.example.org", None, "t21_group", 0.7, 0.9, 300, False,
                    "rss")
BODY = (
    "A strong earthquake struck eastern Taiwan early on Wednesday, halting production lines "
    "at several chip plants while engineers inspected equipment for damage. Officials said "
    "power had been restored to the main science parks by midday and that no major "
    "structural damage was reported. SanDisk said its suppliers in the region were assessing "
    "the impact on flash memory output, and analysts expected short delays rather than "
    "lasting shortages. Shipping through the main ports continued normally."
)


WORDS = ("output", "lines", "wafers", "ports", "freight", "grid", "power", "orders", "prices",
         "capacity", "inventory", "demand", "shipments", "yields", "contracts", "tools",
         "labour", "rates", "exports", "imports", "margins", "guidance", "capital", "plans")


def unique_body():
    """A body far from every other test's body, so near-duplicate checks stay isolated."""
    import random

    rng = random.Random(uuid4().int)
    filler = " ".join(rng.choice(WORDS) + (" " + uuid4().hex[:6]) for _ in range(120))
    return f"Taiwan update. SanDisk said suppliers were assessing impact. {filler}."


class Fetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url):
        self.calls.append(url)
        page = self.pages.get(url)
        if isinstance(page, Exception):
            raise page
        if page is None:
            raise BodyFetchRefused("HTTP 404")
        return page


class Embedder:
    provider, model, usd_per_mtok = "fake", "fake-embed", 0.02

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def embed(self, text):
        self.calls.append(text)
        if self.fail:
            raise TimeoutError("provider down")
        return Embedding(tuple([0.01] * EMBEDDING_DIMENSIONS), 1000)


@pytest.fixture(scope="module")
def catalog(migrated_url):
    dsn = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn) as connection:
        sync_sources(connection, [FEED])
        connection.execute(
            """INSERT INTO entities (entity_id, canonical_name, type, status) VALUES
               ('ent_t21_taiwan', 'Taiwan', 'Country', 'live'),
               ('ent_t21_sandisk', 'SanDisk Corp', 'Company', 'catalog')"""
        )
        connection.execute(
            """INSERT INTO entity_aliases VALUES ('taiwan', 'ent_t21_taiwan', 'test', 1, false),
               ('sandisk', 'ent_t21_sandisk', 'test', 1, false)"""
        )
    return dsn


def article(dsn, title, snippet=None):
    url = f"https://news.example.org/{uuid4().hex}"
    with psycopg.connect(dsn) as connection:
        result = MetadataStage(connection).process({
            "url": url, "title": title, "snippet": snippet, "publisher": "t21",
            "published_at": "2026-10-13T08:00:00Z", "source_hint": "t21_feed"})
    return result.article_id, url, result.tier


def stage(dsn, tmp_path, pages, embedder):
    connection = psycopg.connect(dsn)
    fetcher = Fetcher(pages) if pages is not None else None
    return connection, BodyStage(connection, fetcher, LocalBodyStore(tmp_path), embedder), fetcher


def row(dsn, article_id):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """SELECT fetched_at IS NOT NULL, r2_key, simhash IS NOT NULL,
                      vector_dims(embedding), embedding_scope
               FROM articles WHERE id = %s""", (article_id,),
        ).fetchone()


def usage(dsn, component, outcome="success"):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """SELECT count(*), coalesce(sum(quantity), 0), coalesce(sum(estimated_cost_usd), 0)
               FROM usage_records WHERE component = %s AND outcome = %s""",
            (component, outcome),
        ).fetchone()


def test_tier_one_body_is_stored_tagged_embedded_and_metered(catalog, tmp_path):
    article_id, url, tier = article(catalog, "Earthquake strikes Taiwan")
    embeds_before, r2_before = usage(catalog, "embeddings"), usage(catalog, "r2")
    connection, body_stage, _ = stage(catalog, tmp_path, {url: BODY}, Embedder())
    with connection:
        result = body_stage.process(article_id)

    assert tier == 1
    assert (result.fetched, result.embedding, result.body_entities_added) == ("stored", "body", 1)
    fetched, key, has_simhash, dims, scope = row(catalog, article_id)
    assert (fetched, has_simhash, dims, scope) == (True, True, 1024, "body")
    assert (tmp_path / key).read_text() == BODY
    with psycopg.connect(catalog) as check:
        assert check.execute(
            "SELECT entity_id, found_in FROM article_entities WHERE article_id = %s ORDER BY 1",
            (article_id,),
        ).fetchall() == [("ent_t21_sandisk", "body"), ("ent_t21_taiwan", "metadata")]
    embeds, r2 = usage(catalog, "embeddings"), usage(catalog, "r2")
    assert (embeds[0] - embeds_before[0], embeds[1] - embeds_before[1]) == (1, 1000)
    assert float(embeds[2] - embeds_before[2]) == pytest.approx(0.00002)
    assert (r2[0] - r2_before[0], r2[1] - r2_before[1]) == (1, len(BODY.encode()))


def test_a_syndicated_copy_reuses_the_stored_body_and_embedding(catalog, tmp_path):
    first_id, first_url, _ = article(catalog, "Earthquake strikes Taiwan chip plants")
    copy_id, copy_url, _ = article(catalog, "UPDATE 1-Earthquake strikes Taiwan chip plants")
    embedder, body = Embedder(), unique_body()
    connection, body_stage, _ = stage(catalog, tmp_path, {
        first_url: body, copy_url: body + " Reporting by Jane Doe; Editing by John Roe."},
        embedder)
    with connection:
        body_stage.process(first_id)
        files = sorted(tmp_path.rglob("*.txt"))
        result = body_stage.process(copy_id)

    assert (result.fetched, result.embedding) == ("duplicate", "copied")
    assert len(embedder.calls) == 1 and sorted(tmp_path.rglob("*.txt")) == files
    assert row(catalog, copy_id)[1:] == row(catalog, first_id)[1:]


def test_tier_two_embeds_title_and_snippet_without_fetching(catalog, tmp_path):
    article_id, url, tier = article(catalog, "SanDisk expands output", "New flash capacity.")
    embedder = Embedder()
    connection, body_stage, fetcher = stage(catalog, tmp_path, {url: BODY}, embedder)
    with connection:
        result = body_stage.process(article_id)
    assert (tier, result.fetched, result.embedding) == (2, "not_eligible", "title_snippet")
    assert fetcher.calls == [] and embedder.calls == ["SanDisk expands output\nNew flash capacity."]
    assert row(catalog, article_id) == (False, None, False, 1024, "title_snippet")


def test_tier_three_gets_nothing(catalog, tmp_path):
    article_id, url, tier = article(catalog, "Markets drift ahead of the holiday")
    embedder = Embedder()
    connection, body_stage, fetcher = stage(catalog, tmp_path, {url: BODY}, embedder)
    with connection:
        result = body_stage.process(article_id)
    assert (tier, result.fetched, result.embedding) == (3, "not_eligible", "none")
    assert fetcher.calls == [] and embedder.calls == []
    assert row(catalog, article_id) == (False, None, False, None, None)


def test_a_forced_fetch_keeps_the_tier_embedding_policy(catalog, tmp_path):
    # T22's audit sample fetches a tier-3 body: never embedded.
    article_id, url, _ = article(catalog, "Agency posts routine notice")
    embedder = Embedder()
    connection, body_stage, _ = stage(catalog, tmp_path, {url: unique_body()}, embedder)
    with connection:
        result = body_stage.process(article_id, mode="audit")
    assert (result.fetched, result.embedding, result.audited) == ("stored", "none", True)
    assert embedder.calls == [] and row(catalog, article_id)[3:] == (None, None)


def test_refused_and_failed_fetches_leave_the_article_unfetched(catalog, tmp_path):
    refused_id, refused_url, _ = article(catalog, "Taiwan port reopens")
    failed_id, failed_url, _ = article(catalog, "Taiwan rail resumes")
    embedder = Embedder()
    connection, body_stage, _ = stage(catalog, tmp_path, {
        refused_url: BodyFetchRefused("robots.txt disallows this URL"),
        failed_url: TimeoutError("slow site")}, embedder)
    with connection:
        refused = body_stage.process(refused_id)
        failed = body_stage.process(failed_id)
    assert (refused.fetched, failed.fetched) == ("refused", "failed")
    assert embedder.calls == []
    assert row(catalog, refused_id)[0] is False and row(catalog, failed_id)[0] is False


def test_missing_or_failing_embedder_still_stores_the_body(catalog, tmp_path):
    unconfigured_id, unconfigured_url, _ = article(catalog, "Taiwan exports climb")
    failing_id, failing_url, _ = article(catalog, "Taiwan exports slip")
    failures_before = usage(catalog, "embeddings", "failure")[0]
    connection, body_stage, _ = stage(catalog, tmp_path, {unconfigured_url: unique_body()},
                                      None)
    with connection:
        assert body_stage.process(unconfigured_id).embedding == "not_configured"
    connection, body_stage, _ = stage(catalog, tmp_path, {failing_url: unique_body()},
                                      Embedder(fail=True))
    with connection:
        assert body_stage.process(failing_id).embedding == "failed"
    assert row(catalog, unconfigured_id)[:2] != (False, None)
    assert usage(catalog, "embeddings", "failure")[0] == failures_before + 1


def unsampled_url():
    from app.ingest import url_hash
    from app.quarantine import audit_sampled

    while True:
        url = f"https://news.example.org/{uuid4().hex}"
        if not audit_sampled(url_hash(url)):
            return url


def test_metadata_stage_hands_tier_one_and_two_to_the_body_stage(catalog, monkeypatch):
    from app import bodies
    from app.discovery import process_candidate

    queued = []
    monkeypatch.setattr(bodies.process_article, "delay", lambda *args: queued.append(args))
    monkeypatch.setenv("TEVARTA_POSTGRES_URL", catalog)

    def run(title):
        return process_candidate.run({
            "url": unsampled_url(), "title": title, "snippet": None, "publisher": "t21",
            "published_at": "2026-10-13T08:00:00Z", "source_hint": "t21_feed"})

    tier_one, tier_two, tier_three = (run("Taiwan floods"), run("SanDisk news"),
                                      run("Quiet markets"))
    assert queued == [(tier_one["article_id"], "default"), (tier_two["article_id"], "default")]
    assert (tier_three["tier"], tier_three["body_mode"]) == (3, None)
