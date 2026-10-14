"""T23 expansion-request lifecycle against a migrated PostgreSQL database."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from app.contracts import SeedCreateRequest
from app.expansion import (
    MAX_ATTEMPTS,
    claim_requests,
    finish_request,
    request_republish,
)
from app.onboarding import PostgresOnboardingStore
from conftest import alembic_config
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine

pytestmark = pytest.mark.database


@pytest.fixture
def entities(migrated_dsn):
    """Fresh request queue and catalog entities, committed for multi-connection tests."""
    ids = [f"ent_t23_{index}_{uuid4().hex[:8]}" for index in range(6)]
    with psycopg.connect(migrated_dsn) as connection:
        connection.execute("DELETE FROM entity_expansion_requests")
        connection.execute("DELETE FROM entity_graph_views")
        for entity_id in ids:
            connection.execute(
                """INSERT INTO entities (entity_id, canonical_name, type)
                   VALUES (%s, %s, 'Company')""", (entity_id, entity_id),
            )
    yield ids
    with psycopg.connect(migrated_dsn) as connection:
        connection.execute("DELETE FROM entity_expansion_requests")
        connection.execute("DELETE FROM entity_graph_views")
        connection.execute("DELETE FROM entities WHERE entity_id = ANY(%s)", (ids,))


def request(dsn, *entity_ids):
    with psycopg.connect(dsn) as connection:
        for entity_id in entity_ids:
            connection.execute(
                "INSERT INTO entity_expansion_requests (entity_id) VALUES (%s)", (entity_id,)
            )


def state(dsn, entity_id):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """SELECT status, attempts, last_error FROM entity_expansion_requests
               WHERE entity_id = %s""", (entity_id,),
        ).fetchone()


def ids(claims):
    return [claim.entity_id for claim in claims]


def test_concurrent_dispatchers_claim_each_request_once(migrated_dsn, entities):
    request(migrated_dsn, *entities)

    def claim(_):
        with psycopg.connect(migrated_dsn) as connection:
            return ids(claim_requests(connection, 2))

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = [entity for batch in pool.map(claim, range(8)) for entity in batch]

    assert sorted(claimed) == sorted(entities)
    assert {state(migrated_dsn, entity)[:2] for entity in entities} == {("running", 1)}


def test_failures_retry_with_backoff_until_the_attempt_cap(migrated_dsn, entities):
    target = entities[0]
    request(migrated_dsn, target)
    with psycopg.connect(migrated_dsn) as connection:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            (claim,) = claim_requests(connection, 5)
            assert finish_request(connection, claim, "GraphWriteError") == "failed"
            assert state(migrated_dsn, target) == ("failed", attempt, "GraphWriteError")
            assert claim_requests(connection, 5) == []  # still inside the backoff
            connection.execute(
                """UPDATE entity_expansion_requests
                   SET completed_at = now() - interval '1 day' WHERE entity_id = %s""",
                (target,),
            )
            connection.commit()
        assert claim_requests(connection, 5) == []  # cap reached


def test_a_failed_view_appears_only_after_the_last_retry(migrated_dsn, entities):
    first_build, published = entities[:2]
    request(migrated_dsn, first_build, published)
    view = {"nodes": [{"id": published, "name": "P", "entity_type": "company"}], "edges": []}
    with psycopg.connect(migrated_dsn) as connection:
        connection.execute(
            "INSERT INTO entity_graph_views VALUES (%s, 'ready', %s, now())",
            (published, Jsonb(view)),
        )
        connection.commit()

        def views():
            return dict(connection.execute(
                "SELECT entity_id, status FROM entity_graph_views WHERE entity_id = ANY(%s)",
                ([first_build, published],),
            ).fetchall())

        for attempt in range(1, MAX_ATTEMPTS + 1):
            for claim in claim_requests(connection, 5):
                finish_request(connection, claim, "ServiceUnavailable")
            # A transient first failure keeps the user on "building", not "failed".
            expected = {published: "ready"}
            if attempt == MAX_ATTEMPTS:
                expected[first_build] = "failed"
            assert views() == expected
            connection.execute(
                "UPDATE entity_expansion_requests SET completed_at = now() - interval '1 day'"
            )
            connection.commit()


def test_a_republish_during_a_run_is_not_lost(migrated_dsn, entities):
    target = entities[0]
    request(migrated_dsn, target)
    with psycopg.connect(migrated_dsn) as connection:
        connection.execute(
            "INSERT INTO entity_graph_views VALUES (%s, 'ready', %s, now())",
            (target, Jsonb({"nodes": [], "edges": []})),
        )
        connection.commit()
        (claim,) = claim_requests(connection, 5)
        # Curated edges change while the expansion is reading the old graph.
        assert request_republish(connection) == 1
        assert state(migrated_dsn, target)[0] == "running"
        assert finish_request(connection, claim) == "pending"
        assert ids(claim_requests(connection, 5)) == [target]


def test_a_stale_owner_cannot_finish_a_reclaimed_request(migrated_dsn, entities):
    target = entities[0]
    request(migrated_dsn, target)
    with psycopg.connect(migrated_dsn) as connection:
        (stale,) = claim_requests(connection, 1)
        assert claim_requests(connection, 1) == []
        connection.execute(
            """UPDATE entity_expansion_requests SET claimed_at = now() - interval '1 hour'
               WHERE entity_id = %s""", (target,),
        )
        connection.commit()
        (current,) = claim_requests(connection, 1)
        assert finish_request(connection, stale) is None
        assert state(migrated_dsn, target)[:2] == ("running", 2)
        assert finish_request(connection, current) == "complete"


def test_republish_requeues_finished_views(migrated_dsn, entities):
    done, unpublished = entities[:2]
    request(migrated_dsn, done, unpublished)
    with psycopg.connect(migrated_dsn) as connection:
        connection.execute(
            "INSERT INTO entity_graph_views VALUES (%s, 'ready', %s, now())",
            (done, Jsonb({"nodes": [], "edges": []})),
        )
        connection.commit()
        for claim in claim_requests(connection, 5):
            finish_request(connection, claim)
        assert request_republish(connection) == 1
    assert state(migrated_dsn, done)[:2] == ("pending", 0)
    assert state(migrated_dsn, unpublished)[0] == "complete"


def test_seeding_again_revives_a_request_that_exhausted_its_retries(migrated_dsn, entities):
    target = entities[0]
    user = "usr_t23_" + uuid4().hex
    with psycopg.connect(migrated_dsn) as connection:
        connection.execute("INSERT INTO users (public_id, apple_sub) VALUES (%s, %s)", (user, user))
        connection.execute(
            """INSERT INTO entity_expansion_requests (entity_id, status, attempts, completed_at)
               VALUES (%s, 'failed', %s, now())""", (target, MAX_ATTEMPTS),
        )
    try:
        PostgresOnboardingStore(migrated_dsn).create_seed(
            user, SeedCreateRequest(entity_id=target, weight=1.0, source="manual"))
        assert state(migrated_dsn, target)[:2] == ("pending", 0)
    finally:
        with psycopg.connect(migrated_dsn) as connection:
            connection.execute("DELETE FROM users WHERE public_id = %s", (user,))
            connection.execute(
                "UPDATE entities SET seed_count = 0 WHERE entity_id = %s", (target,)
            )


def test_expansion_lifecycle_migration_roundtrip(database_factory):
    url = database_factory()
    engine = create_engine(url)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
        command.downgrade(alembic_config(connection), "0004_onboarding_handoff")
        command.upgrade(alembic_config(connection), "head")
    engine.dispose()
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn) as connection:
        columns = {row[0] for row in connection.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'entity_expansion_requests'"""
        ).fetchall()}
    assert {"status", "attempts", "claimed_at", "completed_at", "last_error",
            "generation"} <= columns
