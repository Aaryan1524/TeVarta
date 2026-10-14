"""Phase 2 integration proof: a real seed builds a shared live graph, and a real
upstream headline then reaches tier 1. Needs PostgreSQL and Neo4j."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from os import environ
from uuid import uuid4

import psycopg
import pytest
from app.contracts import SeedCreateRequest
from app.discovery import SourceConfig
from app.expansion import EntityExpander, Industry, dispatch
from app.graph_store import GraphStore, TemporalEdgeRevision
from app.ingest import MetadataStage, sync_sources
from app.onboarding import PostgresOnboardingStore
from neo4j import GraphDatabase

pytestmark = [pytest.mark.database, pytest.mark.neo4j]

FEED = SourceConfig("p2_feed", "example.org", None, "p2_group", 0.7, 0.9, 300, False, "rss")


@pytest.fixture(scope="module")
def graph():
    uri = environ.get("TEVARTA_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("Set TEVARTA_TEST_NEO4J_URI to run the Phase 2 integration proof")
    driver = GraphDatabase.driver(uri, auth=(environ.get("TEVARTA_TEST_NEO4J_USER", "neo4j"),
                                             environ["TEVARTA_TEST_NEO4J_PASSWORD"]))
    store = GraphStore(driver, database=environ.get("TEVARTA_TEST_NEO4J_DATABASE", "neo4j"))
    store.initialize()
    yield store
    driver.close()


class FixedIndustry:
    def __init__(self):
        self.calls = 0

    def lookup(self, cik):
        self.calls += 1
        return Industry("3674", "SEMICONDUCTORS &amp; RELATED DEVICES")


@pytest.fixture
def world(migrated_dsn, graph):
    """TSMC, Taiwan, Apple and NVIDIA in the catalog and graph; all catalog-only."""
    suffix = uuid4().hex[:10]
    ids = {name: f"ent_p2_{name}_{suffix}" for name in ("tsmc", "taiwan", "apple", "nvda")}
    users = [f"usr_p2_{n}_{suffix}" for n in "ab"]
    with psycopg.connect(migrated_dsn) as connection:
        sync_sources(connection, [FEED])
        rows = [(ids["tsmc"], "Taiwan Semiconductor Manufacturing Co Ltd", "Company", "0001046179"),
                (ids["taiwan"], "Taiwan", "Country", None),
                (ids["apple"], "Apple Inc.", "Company", None),
                (ids["nvda"], "NVIDIA Corp", "Company", None)]
        for entity_id, name, kind, cik in rows:
            connection.execute(
                """INSERT INTO entities (entity_id, canonical_name, type, cik)
                   VALUES (%s, %s, %s, %s)""", (entity_id, name, kind, cik),
            )
        connection.execute(
            """INSERT INTO entity_aliases VALUES
               ('taiwan', %s, 'test', 1, false),
               ('taiwansemiconductormanufacturing', %s, 'test', 1, false)""",
            (ids["taiwan"], ids["tsmc"]),
        )
        for user in users:
            connection.execute(
                "INSERT INTO users (public_id, apple_sub) VALUES (%s, %s)", (user, user)
            )
    for entity_id, name, kind, _ in rows:
        graph.upsert_entity(entity_id, name, kind)
    observed = datetime(2026, 10, 13, tzinfo=UTC)
    for source, relation, target, weight in [
        ("apple", "DEPENDS_ON", "tsmc", 0.9), ("nvda", "DEPENDS_ON", "tsmc", 0.9),
        ("tsmc", "EXPOSED_TO", "taiwan", 0.8),
    ]:
        graph.create_temporal_edge_revision(TemporalEdgeRevision(
            uuid4(), ids[source], ids[target], relation, weight, 0.8,
            datetime(2025, 1, 1, tzinfo=UTC), observed, ("fixture:p2",), 1, 1.0, "structured",
        ))
    yield ids, users
    with psycopg.connect(migrated_dsn) as connection:
        connection.execute("DELETE FROM users WHERE public_id = ANY(%s)", (users,))
        connection.execute("DELETE FROM entity_expansion_requests")
        connection.execute("DELETE FROM entity_graph_views")


def status(dsn, entity_id):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            "SELECT status FROM entities WHERE entity_id = %s", (entity_id,)
        ).fetchone()[0]


def test_first_seed_builds_one_shared_live_graph_and_unlocks_tier_one(
    migrated_dsn, graph, world
):
    ids, (user_a, user_b) = world
    store = PostgresOnboardingStore(migrated_dsn)
    seed = SeedCreateRequest(entity_id=ids["tsmc"], weight=1.0, source="manual")
    quake = {"url": f"https://example.org/{uuid4().hex}", "title":
             "Strong earthquake strikes eastern Taiwan", "snippet": None, "publisher": "p2",
             "published_at": "2026-10-13T08:00:00Z", "source_hint": "p2_feed"}

    # Before expansion Taiwan is catalog-only: an upstream headline is tier 2.
    with psycopg.connect(migrated_dsn) as connection:
        assert MetadataStage(connection).process(dict(quake)).tier == 2

    store.create_seed(user_a, seed)
    assert store.status(user_a).status == "building"

    industry = FixedIndustry()

    def run(_):
        with psycopg.connect(migrated_dsn) as connection:
            return dispatch(connection, EntityExpander(connection, graph, industry))

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = [item for batch in pool.map(run, range(4)) for item in batch]
    assert [(o["entity_id"], o["status"]) for o in outcomes] == [(ids["tsmc"], "complete")]
    assert outcomes[0]["industry"] == "added" and industry.calls == 1

    # The seed and everything upstream of it is live; a co-customer is not.
    assert status(migrated_dsn, ids["tsmc"]) == "live"
    assert status(migrated_dsn, ids["taiwan"]) == "live"
    assert status(migrated_dsn, "ent_sic_3674") == "live"
    assert status(migrated_dsn, ids["nvda"]) == "catalog"
    assert status(migrated_dsn, ids["apple"]) == "catalog"

    assert store.status(user_a).status == "ready"
    view = store.graph(user_a)
    names = {node.id: (node.name, node.entity_type) for node in view.nodes}
    assert names[ids["taiwan"]] == ("Taiwan", "place")
    assert names["ent_sic_3674"] == ("Semiconductors & Related Devices", "industry")
    assert {(e.source_id, e.relation, e.target_id) for e in view.edges} == {
        (ids["tsmc"], "EXPOSED_TO", ids["taiwan"]), (ids["tsmc"], "IN_INDUSTRY", "ent_sic_3674")}

    # Real headlines now reach tier 1: an upstream country, and the company by name.
    with psycopg.connect(migrated_dsn) as connection:
        stage = MetadataStage(connection)
        upstream = stage.process({**quake, "url": f"https://example.org/{uuid4().hex}"})
        named = stage.process({**quake, "url": f"https://example.org/{uuid4().hex}",
                               "title": "Taiwan Semiconductor Manufacturing raises prices"})
    assert (upstream.tier, upstream.entity_ids) == (1, (ids["taiwan"],))
    assert named.tier == 1 and ids["tsmc"] in named.entity_ids

    # A second user's seed reuses the shared graph: no new work, ready at once.
    store.create_seed(user_b, seed)
    assert store.status(user_b).status == "ready"
    with psycopg.connect(migrated_dsn) as connection:
        assert connection.execute(
            "SELECT status, attempts FROM entity_expansion_requests WHERE entity_id = %s",
            (ids["tsmc"],),
        ).fetchone() == ("complete", 1)
        assert dispatch(connection, EntityExpander(connection, graph, industry)) == []


def test_republishing_does_not_duplicate_the_industry_edge(migrated_dsn, graph, world):
    ids, (user_a, _) = world
    PostgresOnboardingStore(migrated_dsn).create_seed(
        user_a, SeedCreateRequest(entity_id=ids["tsmc"], weight=1.0, source="manual"))
    industry = FixedIndustry()
    with psycopg.connect(migrated_dsn) as connection:
        expander = EntityExpander(connection, graph, industry)
        first = expander.expand(ids["tsmc"])
        second = expander.expand(ids["tsmc"])
    assert (first.industry, second.industry) in {("added", "present"), ("present", "present")}
    history = graph.read_edge_history(ids["tsmc"], "ent_sic_3674", "IN_INDUSTRY")
    assert len([record for record in history if record.valid_until is None]) == 1


def test_industry_enrichment_failure_never_blocks_onboarding(migrated_dsn, graph, world):
    from app.graph_store import TemporalEdgeInvalidation

    ids, _ = world
    with psycopg.connect(migrated_dsn) as connection:
        expander = EntityExpander(connection, graph, FixedIndustry())
        expander.expand(ids["tsmc"])
        # Retire the SIC edge; re-adding it with its old valid_from is a stale revision.
        graph.invalidate_temporal_edge(TemporalEdgeInvalidation(
            uuid4(), ids["tsmc"], "ent_sic_3674", "IN_INDUSTRY",
            datetime.now(UTC), datetime.now(UTC), ("fixture:retired",)))
        report = expander.expand(ids["tsmc"])
    assert report.industry == "unavailable"
    with psycopg.connect(migrated_dsn) as connection:
        assert connection.execute(
            "SELECT status FROM entity_graph_views WHERE entity_id = %s", (ids["tsmc"],)
        ).fetchone() == ("ready",)


def test_prewarm_promotes_every_listed_entity_and_reports_gaps(migrated_dsn, graph, world):
    from app.prewarm import prewarm

    ids, _ = world
    with psycopg.connect(migrated_dsn) as connection:
        for name in ("tsmc", "nvda"):
            connection.execute(
                "INSERT INTO entity_aliases VALUES (%s, %s, 'test', 1, true)",
                (f"ticker:p2{name}{ids[name][-6:]}", ids[name]),
            )
        connection.commit()
        expander = EntityExpander(connection, graph, FixedIndustry())
        listed = [ids["tsmc"], ids["nvda"]]
        first = {item.entity_id: item for item in prewarm(connection, expander, listed)}
        again = {item.entity_id: item for item in prewarm(connection, expander, listed)}

    tsmc, nvda = first[ids["tsmc"]], first[ids["nvda"]]
    assert tsmc.live and tsmc.view == "ready" and tsmc.dependency_edges == 1 and not tsmc.gap
    # NVIDIA's only edge is to TSMC, which is upstream: it is reached, so no gap either.
    assert nvda.live and nvda.dependency_edges >= 1
    assert again == first  # idempotent: same views, same coverage
    with psycopg.connect(migrated_dsn) as connection:
        assert connection.execute(
            """SELECT status, count(*) FROM entity_expansion_requests
               WHERE entity_id = ANY(%s) GROUP BY 1""", (listed,),
        ).fetchall() == [("complete", 2)]


def test_prewarm_reports_an_entity_without_dependencies_as_a_gap(migrated_dsn, graph, world):
    from app.prewarm import prewarm

    ids, _ = world
    with psycopg.connect(migrated_dsn) as connection:
        connection.execute(
            """INSERT INTO entities (entity_id, canonical_name, type)
               VALUES ('ent_p2_lonely', 'Lonely Co', 'Company')
               ON CONFLICT (entity_id) DO NOTHING"""
        )
        connection.commit()
        (item,) = prewarm(connection, EntityExpander(connection, graph, None), ["ent_p2_lonely"])
    assert (item.live, item.view, item.dependency_edges, item.gap) == (True, "ready", 0, True)
