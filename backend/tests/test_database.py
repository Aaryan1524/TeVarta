"""Run against disposable PostgreSQL databases, never an existing application schema."""

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app.auth import PostgresSessionStore, SessionService
from app.contracts import FeedbackRequest, SeedCreateRequest
from app.curation import (
    CuratedEntity,
    EndpointNotResolved,
    PostgresCurationCatalog,
    content_sha,
    parse_curation_file,
    parse_review_log,
    plan_curated_load,
    review_batch,
)
from app.curation_draft import DRAFT_COMPONENT, PostgresUsageMeter, TokenPrices
from app.discovery import (
    DiscoveryCandidate,
    PostgresSearchBudgetStore,
    SearchRequest,
    SearchService,
)
from app.entity_resolution import EntityResolver
from app.main import app
from app.onboarding import OnboardingError, PostgresOnboardingStore
from app.operations import FixedWindowRateLimiter, SearchBudgetExceeded
from app.sec_catalog import import_sec_catalog, parse_company_tickers
from app.telemetry import PostgresTelemetryStore, UnknownEvent
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.database
USER_A = "00000000-0000-0000-0000-000000000001"
EVENT = "00000000-0000-0000-0000-000000000004"
OTHER_EVENT = "00000000-0000-0000-0000-000000000005"
TABLES = {
    "users",
    "devices",
    "user_seeds",
    "entities",
    "entity_aliases",
    "sources",
    "articles",
    "article_entities",
    "quarantine",
    "recall_audit",
    "events",
    "event_articles",
    "event_entities",
    "event_summaries",
    "why_cache",
    "user_feed",
    "score_distribution",
    "notification_log",
    "feedback",
    "resolution_misses",
    "demand_log",
    "event_claims",
    "usage_records",
    "auth_sessions",
    "search_query_reservations",
    "search_budget_alerts",
    "entity_expansion_requests",
    "entity_graph_views",
}


def alembic_config(connection=None):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="module")
def database_factory():
    target = os.environ.get("TEVARTA_TEST_POSTGRES_URL")
    if not target:
        pytest.skip("Set TEVARTA_TEST_POSTGRES_URL to run actual PostgreSQL migration tests")
    url = make_url(target)
    if url.host not in {"localhost", "127.0.0.1"} or url.database != "tevarta_test":
        pytest.fail("Database tests require localhost and the dedicated tevarta_test database")
    admin_dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    owned = []

    def create():
        name = "tevarta_test_" + uuid4().hex
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        owned.append(name)
        return url.set(drivername="postgresql+psycopg", database=name)

    yield create
    # Only databases created by this fixture are removed; no wildcard or reused data.
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        for name in owned:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture(scope="module")
def migrated_url(database_factory):
    url = database_factory()
    engine = create_engine(url)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    engine.dispose()
    return url


@pytest.fixture
def db(migrated_url):
    dsn = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn) as connection:
        connection.execute((ROOT / "fixtures/database/foundation.sql").read_text())
        yield connection
        connection.rollback()


def test_all_tables_and_critical_indexes_exist(db):
    tables = {
        row[0] for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert tables == TABLES | {"alembic_version"}
    indexes = dict(
        db.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
    )
    assert "(entity_id)" in indexes["ix_user_seeds_entity"]
    assert "(user_id, score DESC, created_at DESC)" in indexes["ix_user_feed_rank"]
    assert "UNIQUE" in indexes["uq_entity_alias_identifier"]
    assert "WHERE is_identifier" in indexes["uq_entity_alias_identifier"]


def test_alias_can_have_multiple_candidates_but_identifier_cannot(db):
    assert (
        db.execute("SELECT count(*) FROM entity_aliases WHERE alias_norm = 'apple'").fetchone()[0]
        == 2
    )
    db.execute(
        """INSERT INTO entity_aliases VALUES ('nasdaq:aapl', 'ent_apple', 'fixture', 1, true)"""
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            """INSERT INTO entity_aliases VALUES ('nasdaq:aapl', 'ent_fruit', 'fixture', 1, true)"""
        )


def test_sec_catalog_reimport_is_idempotent_and_keeps_entities_catalog(db):
    companies = parse_company_tickers(
        {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 1000184, "ticker": "SNDK", "title": "SanDisk Corporation"},
        }
    )

    first = import_sec_catalog(db, companies)
    second = import_sec_catalog(db, companies)

    assert first == second
    entity_id = "ent_sec_0000320193"
    status = db.execute(
        "SELECT status FROM entities WHERE entity_id = %s", (entity_id,)
    ).fetchone()[0]
    alias_count = db.execute(
        "SELECT count(*) FROM entity_aliases WHERE entity_id = %s", (entity_id,)
    ).fetchone()[0]
    assert status == "catalog"
    assert alias_count == 3


def test_entity_resolver_logs_unresolved_aliases_without_tagging(db):
    db.execute(
        "INSERT INTO entities (entity_id, canonical_name, type, seed_count) VALUES "
        "('ent_mercury_a', 'Mercury A', 'Company', 1), "
        "('ent_mercury_b', 'Mercury B', 'Company', 1)"
    )
    db.execute(
        "INSERT INTO entity_aliases VALUES "
        "('mercury', 'ent_mercury_a', 'fixture', 1, false), "
        "('mercury', 'ent_mercury_b', 'fixture', 1, false)"
    )

    result = EntityResolver(db).resolve("Mercury")

    assert result.status == "ambiguous"
    assert result.entity_id is None
    miss_count = db.execute(
        "SELECT count(*) FROM resolution_misses WHERE surface_form = 'Mercury'"
    ).fetchone()[0]
    assert miss_count == 1


@pytest.mark.parametrize("table", ["user_seeds", "user_feed", "feedback", "event_articles"])
def test_duplicate_keys_rejected(db, table):
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            sql.SQL("INSERT INTO {} SELECT * FROM {} LIMIT 1").format(
                sql.Identifier(table), sql.Identifier(table)
            )
        )


@pytest.mark.parametrize(
    "table,column,public_id",
    [
        ("articles", "embedding", "art_test"),
        ("events", "centroid", "evt_test"),
    ],
)
def test_vector_dimensions_enforced(db, table, column, public_id):
    vector = "[" + ",".join(["0.1"] * 1024) + "]"
    scope = sql.SQL(", embedding_scope = 'body'") if table == "articles" else sql.SQL("")
    query = sql.SQL("UPDATE {} SET {} = %s::vector{} WHERE public_id = %s").format(
        sql.Identifier(table), sql.Identifier(column), scope
    )
    db.execute(query, (vector, public_id))
    dimensions = db.execute(
        sql.SQL("SELECT vector_dims({}) FROM {} WHERE public_id = %s").format(
            sql.Identifier(column), sql.Identifier(table)
        ),
        (public_id,),
    ).fetchone()[0]
    assert dimensions == 1024
    with pytest.raises(psycopg.errors.DataException):
        db.execute(query, ("[1,2,3]", public_id))


def test_embedding_scope_cannot_exist_without_embedding(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE articles SET embedding_scope = 'body'")


@pytest.mark.parametrize(
    "key",
    [
        "path_score",
        "importance",
        "freshness",
        "portfolio_exposure",
        "source_reliability",
        "novelty",
        "explicit_interest",
    ],
)
def test_each_logged_component_required(db, key):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE user_feed SET components = components - %s", (key,))


@pytest.mark.parametrize("value", ["null", '"0.5"', "false", "[]", "{}"])
def test_logged_component_must_be_numeric(db, value):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "UPDATE user_feed SET components = jsonb_set(components, '{novelty}', %s::jsonb)",
            (value,),
        )


def test_zero_weight_components_keep_measured_values(db):
    components = db.execute("SELECT components FROM user_feed LIMIT 1").fetchone()[0]
    assert components["source_reliability"] == 0.99
    assert components["portfolio_exposure"] == 0.3


def test_account_delete_cascades_only_user_owned_rows(db):
    db.execute("DELETE FROM users WHERE id = %s", (USER_A,))
    for table in ["user_seeds", "devices", "user_feed", "feedback", "notification_log"]:
        assert (
            db.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE user_id = %s").format(sql.Identifier(table)),
                (USER_A,),
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[
                0
            ]
            == 1
        )
    for table in ["events", "articles", "entities", "why_cache", "event_claims", "usage_records"]:
        assert (
            db.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[
                0
            ]
            > 0
        )


def test_merged_event_reference_is_uuid_and_preserves_loser(db):
    db.execute(
        "UPDATE events SET status = 'merged', merged_into = %s WHERE id = %s", (EVENT, OTHER_EVENT)
    )
    assert (
        str(
            db.execute(
                "SELECT merged_into FROM events WHERE public_id = %s", ("evt_other",)
            ).fetchone()[0]
        )
        == EVENT
    )
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("DELETE FROM events WHERE id = %s", (EVENT,))


def test_self_merge_rejected(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE events SET status = 'merged', merged_into = id")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE events SET severity_class = 'high', severity = 0.99",
        "UPDATE user_seeds SET source = 'unknown'",
        "UPDATE user_seeds SET weight = 1.1",
        "UPDATE sources SET tier_value = 0.8",
        "UPDATE sources SET active = true",
        "UPDATE usage_records SET quantity = -1",
        "UPDATE usage_records SET estimated_cost_usd = 'NaN'",
        "UPDATE feedback SET slot_position = NULL",
        "UPDATE feedback SET action = 'session_length'",
        "UPDATE feedback SET session_duration_s = 'Infinity'",
        "UPDATE user_feed SET shadow_score = 'NaN'",
    ],
)
def test_invalid_values_fail_at_storage_boundary(db, statement):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(statement)


def test_feedback_allows_multiple_actions_with_distinct_idempotency_keys(db):
    db.execute(
        """INSERT INTO feedback
        (user_id, event_id, action, slot_type, slot_position, idempotency_key, ts)
        VALUES (%s, %s, 'click', 'graph', 1, 'fixture-click', now())""",
        (USER_A, EVENT),
    )
    assert (
        db.execute("SELECT count(*) FROM feedback WHERE user_id = %s", (USER_A,)).fetchone()[0] == 2
    )


def test_feedback_retry_rejected_even_with_fresh_row_id(db):
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("""INSERT INTO feedback
            (user_id, event_id, action, slot_type, slot_position, idempotency_key, ts)
            SELECT user_id, event_id, action, slot_type, slot_position, idempotency_key, ts
            FROM feedback LIMIT 1""")


@pytest.mark.parametrize("column", ["shadow_score", "shadow_rank"])
def test_shadow_measurement_cannot_be_omitted(db, column):
    with pytest.raises(psycopg.errors.NotNullViolation):
        db.execute(sql.SQL("UPDATE user_feed SET {} = NULL").format(sql.Identifier(column)))


def test_online_environment_target(database_factory, monkeypatch):
    url = database_factory()
    monkeypatch.setenv("TEVARTA_POSTGRES_URL", url.render_as_string(hide_password=False))
    command.upgrade(alembic_config(), "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
                == "0004_onboarding_handoff"
            )
    finally:
        engine.dispose()


def test_session_store_persists_one_user_and_consumes_refresh_tokens_atomically(migrated_url):
    dsn = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    store = PostgresSessionStore(dsn)
    first_user = store.get_or_create_user("apple-sub-a")
    assert store.get_or_create_user("apple-sub-a") == first_user
    second_user = store.get_or_create_user("apple-sub-b")
    assert second_user.internal_id != first_user.internal_id

    expires_at = datetime.now(UTC) + timedelta(days=1)
    original = store.create_session(first_user.internal_id, b"original-refresh", expires_at)
    replacement = store.replace_session(b"original-refresh", b"replacement-refresh", expires_at)

    assert replacement is not None
    assert replacement.user == first_user
    assert store.find_active_session(original.id) is None
    assert store.replace_session(b"original-refresh", b"another-refresh", expires_at) is None
    assert store.find_active_session(replacement.id) is not None


def test_session_refresh_allows_one_concurrent_consumer_and_rolls_back_on_conflict(migrated_url):
    dsn = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    store = PostgresSessionStore(dsn)
    user = store.get_or_create_user("apple-sub-concurrent")
    expires_at = datetime.now(UTC) + timedelta(days=1)
    store.create_session(user.internal_id, b"concurrent-refresh", expires_at)

    def replace(index: int):
        return PostgresSessionStore(dsn).replace_session(
            b"concurrent-refresh", f"concurrent-replacement-{index}".encode(), expires_at
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        replacements = list(executor.map(replace, range(2)))
    assert sum(replacement is not None for replacement in replacements) == 1

    original = store.create_session(user.internal_id, b"rollback-refresh", expires_at)
    store.create_session(user.internal_id, b"conflicting-replacement", expires_at)
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.replace_session(b"rollback-refresh", b"conflicting-replacement", expires_at)
    assert store.find_active_session(original.id) is not None
    assert store.replace_session(b"rollback-refresh", b"valid-replacement", expires_at) is not None


def test_telemetry_store_rejects_unknown_card_event(migrated_url):
    dsn = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    store = PostgresTelemetryStore(dsn)
    user = PostgresSessionStore(dsn).get_or_create_user("apple-sub-telemetry")
    event = FeedbackRequest.model_validate(
        {
            "event_id": "evt_missing",
            "action": "card_click",
            "slot_type": "graph",
            "slot_position": 1,
            "occurred_at": "2026-09-14T00:00:00Z",
            "idempotency_key": "unknown-event-key",
        }
    )

    with pytest.raises(UnknownEvent):
        store.record(user.public_id, event)


def test_claim_requires_existing_evidence(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("UPDATE event_claims SET article_id = %s", (uuid4(),))


def test_usage_request_deduplication(db):
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("""INSERT INTO usage_records
            (request_id, component, outcome, quantity, unit, estimated_cost_usd)
            SELECT request_id, component, outcome, quantity, unit, estimated_cost_usd
            FROM usage_records""")


def test_search_budget_is_shared_across_concurrent_workers_and_alerts_once(migrated_url):
    dsn = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    alerts: list[dict[str, object]] = []
    now = datetime(2026, 9, 20, tzinfo=UTC)

    def reserve(index: int):
        store = PostgresSearchBudgetStore(dsn, monthly_query_cap=5)
        reservation = store.reserve(uuid4(), now)
        if reservation.alert_80_percent:
            alerts.append({"event": "search_cap_80_percent", "used": reservation.used})
        return reservation

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(reserve, index) for index in range(8)]
    reservations = []
    blocked = 0
    for future in futures:
        try:
            reservations.append(future.result())
        except SearchBudgetExceeded:
            blocked += 1

    assert len(reservations) == 5
    assert blocked == 3
    assert sum(reservation.alert_80_percent for reservation in reservations) == 1
    assert alerts == [{"event": "search_cap_80_percent", "used": 4}]


def test_search_usage_receipts_track_success_and_blocked_requests(migrated_url):
    dsn = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    now = datetime(2026, 10, 20, tzinfo=UTC)

    class Provider:
        def search(self, _request):
            return (
                DiscoveryCandidate(
                    "https://example.invalid/result",
                    "Synthetic result",
                    None,
                    "fixture",
                    now,
                    "fixture",
                ),
            )

    service = SearchService(Provider(), PostgresSearchBudgetStore(dsn, monthly_query_cap=1))
    service.search(SearchRequest(uuid4(), "Apple", now - timedelta(hours=24), 1), now)
    with pytest.raises(SearchBudgetExceeded):
        service.search(SearchRequest(uuid4(), "SanDisk", now - timedelta(hours=24), 1), now)

    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT outcome, quantity, query_count FROM usage_records ORDER BY occurred_at"
        ).fetchall()
    assert rows == [("success", 1, 1), ("blocked", 0, 0)]


TAIWAN = CuratedEntity("ent_cur_country_taiwan", "Taiwan", "Country", ("Taiwan",))


def test_curation_catalog_resolves_strictly_without_logging_misses(db):
    import_sec_catalog(
        db,
        parse_company_tickers(
            {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        ),
    )
    catalog = PostgresCurationCatalog(db)
    catalog.upsert_curated_entities([TAIWAN])
    catalog.upsert_curated_entities([TAIWAN])  # idempotent

    by_ticker = catalog.resolve("AAPL")
    by_alias = catalog.resolve("Taiwan")
    by_id = catalog.resolve("ent_cur_country_taiwan")

    assert (by_ticker.entity_id, by_ticker.resolution_quality) == ("ent_sec_0000320193", 1.0)
    assert (by_alias.entity_id, by_alias.entity_type, by_alias.resolution_quality) == (
        "ent_cur_country_taiwan", "Country", 0.7
    )
    assert by_id.resolution_quality == 1.0
    with pytest.raises(EndpointNotResolved, match="ambiguous"):
        catalog.resolve("Apple")  # fixture company, fixture fruit topic, and SEC Apple
    with pytest.raises(EndpointNotResolved, match="did not resolve"):
        catalog.resolve("TSMC")
    with pytest.raises(EndpointNotResolved, match="not a known entity_id"):
        catalog.resolve("ent_cur_missing")
    assert db.execute("SELECT count(*) FROM resolution_misses").fetchone()[0] == 0
    assert db.execute(
        "SELECT source, is_identifier FROM entity_aliases WHERE entity_id = %s",
        (TAIWAN.entity_id,),
    ).fetchall() == [("curated", False)]


def test_reviewed_curated_edge_plans_against_the_real_catalog(db):
    import_sec_catalog(
        db,
        parse_company_tickers(
            {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        ),
    )
    catalog = PostgresCurationCatalog(db)
    catalog.upsert_curated_entities([TAIWAN])
    evidence = {
        "url": "https://www.sec.gov/Archives/edgar/data/320193/aapl-20250927.htm",
        "source_type": "sec_filing",
        "publisher_group": "apple",
    }
    batch = {
        "batch_id": "aapl-20260928",
        "drafted_by": "human",
        "drafted_at": "2026-09-28T10:00:00+00:00",
        "usage_request_id": None,
        "subject": "AAPL",
        "evidence": [evidence],
        "proposals": [
            {
                "proposal_id": "aapl-20260928-01",
                "draft": None,
                "draft_sha": None,
                "decision": "accept",
                "reason": None,
                "edge": {
                    "source": "AAPL", "relation": "EXPOSED_TO", "target": "Taiwan",
                    "weight": 0.8, "directness": 1.0,
                    "rationale": "Filing discloses manufacturing concentration in Taiwan",
                    "evidence": [evidence], "valid_from": "2025-09-27",
                    "review_by": "2027-03-28",
                },
            }
        ],
    }
    empty = parse_curation_file({"entities": [], "edges": []})
    outcome = review_batch(batch, empty, parse_review_log([]), "aaryan", date(2026, 9, 28))
    log = parse_review_log(json.dumps(record.as_json()) for record in outcome.decided)

    [planned] = plan_curated_load(outcome.curation, log, catalog)

    assert planned.revision.from_entity_id == "ent_sec_0000320193"
    assert planned.revision.to_entity_id == "ent_cur_country_taiwan"
    # SEC filing (1.0), one publisher group, stated, alias-resolved endpoint (0.7).
    assert planned.revision.confidence == pytest.approx(0.35 + 0.25 / 3 + 0.25 + 0.105)
    assert planned.revision.source_ids == (f"curated:{content_sha(outcome.decided[0].edge)}",)


def test_curation_draft_meter_records_a_costed_redacted_receipt(database_factory):
    url = database_factory()
    engine = create_engine(url)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    engine.dispose()
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    meter = PostgresUsageMeter(dsn, TokenPrices(Decimal("2"), Decimal("8")))
    request_id = uuid4()

    meter.check_available()
    for _ in range(2):  # a retried receipt write never double-counts
        meter.record(
            request_id, "success", provider="openai", model="configured-model",
            input_tokens=500_000, output_tokens=50_000, latency_ms=1234,
        )

    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            """SELECT component, outcome, quantity, unit, estimated_cost_usd, provider,
                      model, input_tokens, output_tokens, latency_ms
               FROM usage_records WHERE request_id = %s""",
            (request_id,),
        ).fetchall()
    assert rows == [
        (DRAFT_COMPONENT, "success", 1, "call", Decimal("1.4"), "openai", "configured-model",
         500_000, 50_000, 1234)
    ]


def test_upgrade_repeated_then_downgrade_and_reupgrade(database_factory):
    engine = create_engine(database_factory())
    try:
        with engine.begin() as connection:
            config = alembic_config(connection)
            command.upgrade(config, "head")
            connection.exec_driver_sql((ROOT / "fixtures/database/foundation.sql").read_text())
        with engine.begin() as connection:
            config = alembic_config(connection)
            command.upgrade(config, "head")
            assert connection.exec_driver_sql("SELECT count(*) FROM users").scalar() == 2
        with engine.begin() as connection:
            config = alembic_config(connection)
            command.downgrade(config, "base")
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename <> 'alembic_version'"
                ).scalar()
                == 0
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
                ).scalar()
                == 1
            )
        with engine.begin() as connection:
            config = alembic_config(connection)
            command.upgrade(config, "head")
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
                == "0004_onboarding_handoff"
            )
    finally:
        engine.dispose()


def test_offline_sql_contains_transaction_and_vector(monkeypatch):
    monkeypatch.setenv("TEVARTA_POSTGRES_URL", "postgresql://localhost/tevarta_test")
    config = alembic_config()
    config.output_buffer = io.StringIO()
    command.upgrade(config, "head", sql=True)
    rendered = config.output_buffer.getvalue()
    assert "BEGIN;" in rendered and "COMMIT;" in rendered
    assert "VECTOR(1024)" in rendered


def test_missing_target_fails_without_connecting(monkeypatch):
    monkeypatch.delenv("TEVARTA_POSTGRES_URL", raising=False)
    with pytest.raises(RuntimeError, match="explicitly"):
        command.upgrade(alembic_config(), "head")


@pytest.fixture
def onboarding_db(migrated_url):
    """Committed isolated identities; store calls intentionally use separate connections."""
    dsn = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    suffix = uuid4().hex
    users = ["usr_t25a_" + suffix, "usr_t25b_" + suffix]
    entities = ["ent_t25a_" + suffix, "ent_t25b_" + suffix]
    with psycopg.connect(dsn) as connection:
        for user in users:
            connection.execute(
                "INSERT INTO users(public_id, apple_sub) VALUES (%s, %s)", (user, user)
            )
        for index, entity in enumerate(entities):
            connection.execute(
                """INSERT INTO entities(entity_id, canonical_name, type, ticker)
                   VALUES (%s, %s, 'Company', %s)""",
                (entity, "T25 Company " + str(index), "T25" + str(index)),
            )
            connection.execute(
                """INSERT INTO entity_aliases VALUES (%s, %s, 'test', 1, false),
                   (%s, %s, 'test', 1, true)""",
                ("t25apple", entity, "ticker:t25" + str(index), entity),
            )
    yield PostgresOnboardingStore(dsn), dsn, users, entities
    with psycopg.connect(dsn) as connection:
        connection.execute("DELETE FROM users WHERE public_id = ANY(%s)", (users,))
        connection.execute("DELETE FROM entities WHERE entity_id = ANY(%s)", (entities,))


def t25_seed(entity):
    return SeedCreateRequest(entity_id=entity, weight=0.8, source="manual")


def t25_publish(dsn, entity, graph):
    # Simulate T23's atomic completion; no graph traversal belongs in T25.
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE entities SET status = 'live' WHERE entity_id = %s", (entity,))
        connection.execute(
            """INSERT INTO entity_graph_views(entity_id, status, graph_json)
               VALUES (%s, 'ready', %s) ON CONFLICT (entity_id)
               DO UPDATE SET status = 'ready', graph_json = EXCLUDED.graph_json""",
            (entity, Jsonb(graph)),
        )


def t25_graph(entity, index=0):
    return {"nodes": [{"id": entity, "name": "T25 Company " + str(index),
                       "entity_type": "Company"}], "edges": []}


def test_onboarding_search_retains_ambiguity_and_supports_prefix_and_ticker(onboarding_db):
    store, dsn, _, entities = onboarding_db
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE entities SET seed_count = 10 WHERE entity_id = %s", (entities[0],)
        )
    result = store.search("T25 Apple")
    assert result.ambiguous
    assert {item.id for item in result.candidates} == set(entities)
    assert store.search("T25").ambiguous is False
    assert len(store.search("T25").candidates) == 2
    assert store.search("NASDAQ:T250").candidates[0].id == entities[0]
    assert store.search("%_").candidates == []
    assert store.search("absententity").candidates == []


def test_onboarding_concurrent_seeds_count_once_and_share_durable_work(onboarding_db):
    store, dsn, users, entities = onboarding_db
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda index: store.create_seed(users[index % 2], t25_seed(entities[0])), range(24)
        ))
    assert sum(result.created for result in results) == 2
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT seed_count, status FROM entities WHERE entity_id = %s", (entities[0],)
        ).fetchone() == (2, "catalog")
        assert connection.execute(
            "SELECT count(*) FROM entity_expansion_requests WHERE entity_id = %s", (entities[0],)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM feedback f JOIN users u ON u.id = f.user_id "
            "WHERE u.public_id = ANY(%s) AND action = 'seed_added'", (users,)
        ).fetchone()[0] == 2
    # Persisted work/status survives constructing a fresh API adapter.
    assert PostgresOnboardingStore(dsn).status(users[0]).status == "building"
    assert store.entity(entities[0]).graph_status == "building"
    retry = store.create_seed(users[0], SeedCreateRequest(
        entity_id=entities[0], weight=0.1, source="broker"
    ))
    assert not retry.created and retry.seed.weight == 0.8 and retry.seed.source == "manual"


def test_onboarding_delete_is_idempotent_and_never_removes_other_users_seed(onboarding_db):
    store, dsn, users, entities = onboarding_db
    for user in users:
        store.create_seed(user, t25_seed(entities[0]))
    for _ in range(3):
        store.delete_seed(users[0], entities[0])
    assert store.graph(users[0]).nodes == []
    assert store.status(users[0]).status == "not_started"
    assert store.graph(users[1]).nodes[0].id == entities[0]
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT seed_count FROM entities WHERE entity_id = %s", (entities[0],)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM entity_expansion_requests WHERE entity_id = %s", (entities[0],)
        ).fetchone()
        assert connection.execute(
            "SELECT count(*) FROM feedback f JOIN users u ON u.id=f.user_id "
            "WHERE u.public_id = %s AND action='seed_removed'", (users[0],)
        ).fetchone()[0] == 1


def test_onboarding_add_delete_race_preserves_exact_seed_count(onboarding_db):
    store, dsn, users, entities = onboarding_db
    def mutate(index):
        user = users[index % 2]
        if index % 3:
            store.create_seed(user, t25_seed(entities[0]))
        else:
            store.delete_seed(user, entities[0])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(mutate, range(30)))
    with psycopg.connect(dsn) as connection:
        count = connection.execute(
            "SELECT count(*) FROM user_seeds WHERE entity_id = %s", (entities[0],)
        ).fetchone()[0]
        assert connection.execute(
            "SELECT seed_count FROM entities WHERE entity_id = %s", (entities[0],)
        ).fetchone()[0] == count


def test_onboarding_transaction_rolls_back_seed_count_work_and_telemetry(
    onboarding_db, monkeypatch
):
    store, dsn, users, entities = onboarding_db
    def broken_record(*_):
        raise RuntimeError("injected receipt failure")
    monkeypatch.setattr(store, "_record_mutation", broken_record)
    with pytest.raises(RuntimeError, match="receipt failure"):
        store.create_seed(users[0], t25_seed(entities[0]))
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT seed_count FROM entities WHERE entity_id = %s", (entities[0],)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT 1 FROM user_seeds WHERE entity_id = %s", (entities[0],)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM entity_expansion_requests WHERE entity_id = %s", (entities[0],)
        ).fetchone() is None


def test_onboarding_prepared_views_are_scoped_and_ready_requires_live_snapshot(onboarding_db):
    store, dsn, users, entities = onboarding_db
    for user, entity in zip(users, entities, strict=True):
        store.create_seed(user, t25_seed(entity))
    assert store.status(users[0]).status == "building"
    t25_publish(dsn, entities[0], t25_graph(entities[0]))
    assert store.status(users[0]).status == "ready"
    assert store.status(users[1]).status == "building"
    assert store.graph(users[0]).model_dump() == t25_graph(entities[0])
    assert store.graph(users[1]).model_dump() == t25_graph(entities[1], 1)
    # Already prepared shared graphs require no new expansion request for user B.
    store.create_seed(users[1], t25_seed(entities[0]))
    assert len(store.graph(users[1]).nodes) == 2
    store.delete_seed(users[0], entities[0])
    assert store.graph(users[0]).nodes == []
    assert store.entity(entities[0]).graph_status == "live"


def test_onboarding_failed_and_corrupt_views_do_not_report_ready(onboarding_db):
    store, dsn, users, entities = onboarding_db
    store.create_seed(users[0], t25_seed(entities[0]))
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO entity_graph_views(entity_id, status) VALUES (%s, 'failed')",
            (entities[0],),
        )
    assert store.status(users[0]).status == "failed"
    t25_publish(dsn, entities[0], {"nodes": [], "edges": []})
    for read in (store.graph, store.status):
        with pytest.raises(OnboardingError) as error:
            read(users[0])
        assert error.value.status == 503


def test_onboarding_api_uses_real_sessions_and_rejects_identity_injection(
    onboarding_db, monkeypatch
):
    store, dsn, users, entities = onboarding_db
    class Verifier:
        def verify(self, token):
            return token
    auth = SessionService(
        PostgresSessionStore(dsn), Verifier(), "t" * 32, timedelta(days=30)
    )
    monkeypatch.setattr(app.state, "auth_service", auth)
    monkeypatch.setattr(app.state, "onboarding_store", store)
    monkeypatch.setattr(app.state, "rate_limiter", FixedWindowRateLimiter(100, 60))
    client = TestClient(app)
    headers = []
    for user in users:
        result = client.post("/v1/auth/apple", json={"identity_token": user})
        assert result.status_code == 200
        headers.append({"Authorization": "Bearer " + result.json()["access_token"]})
    for method, path, body in [
        ("GET", "/v1/entities/search?q=T25", None),
        ("GET", "/v1/seeds/status", None), ("GET", "/v1/graph", None),
        ("GET", "/v1/entities/" + entities[0], None),
        ("POST", "/v1/seeds", t25_seed(entities[0]).model_dump()),
        ("DELETE", "/v1/seeds/" + entities[0], None),
    ]:
        assert client.request(method, path, json=body).status_code == 401
        assert client.request(method, path, json=body,
                              headers={"Authorization": "Bearer fixture-access-token"}
                              ).status_code == 401
    body = t25_seed(entities[0]).model_dump()
    assert client.post("/v1/seeds", headers=headers[0], json=body | {
        "user_id": users[1]
    }).status_code == 422
    first = client.post("/v1/seeds", headers=headers[0], json=body)
    assert first.status_code == 201 and first.json()["created"]
    assert not client.post("/v1/seeds", headers=headers[0], json=body).json()["created"]
    assert client.get("/v1/graph", headers=headers[1]).json() == {"nodes": [], "edges": []}
    assert client.delete("/v1/seeds/" + entities[0], headers=headers[1]).status_code == 204
    assert client.get("/v1/graph", headers=headers[0]).json()["nodes"][0]["id"] == entities[0]
    assert client.get("/v1/entities/ent_missing", headers=headers[0]).status_code == 404
    assert client.get("/v1/entities/search?q=T25Apple", headers=headers[0]).json()["ambiguous"]
    assert client.post("/v1/auth/sign-out", headers=headers[0]).status_code == 204
    assert client.get("/v1/graph", headers=headers[0]).status_code == 401


def test_onboarding_migration_roundtrip(database_factory):
    url = database_factory()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            config = alembic_config(connection)
            command.upgrade(config, "head")
            command.downgrade(config, "0003_search_budget_reservations")
            assert connection.exec_driver_sql(
                "SELECT to_regclass('entity_expansion_requests')"
            ).scalar() is None
            command.upgrade(config, "head")
            assert connection.exec_driver_sql(
                "SELECT to_regclass('entity_graph_views')"
            ).scalar() == "entity_graph_views"
    finally:
        engine.dispose()
