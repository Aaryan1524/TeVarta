"""Shared disposable-PostgreSQL fixtures for every `database`-marked test module."""

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]


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
def migrated_dsn(migrated_url):
    """psycopg DSN for the module's migrated database."""
    return migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
