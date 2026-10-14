"""T24 alias loading and SEC import aliases against PostgreSQL."""

from uuid import uuid4

import psycopg
import pytest
from app.aliases import AliasEntry, AliasFileError, apply_aliases, resolve_entity
from app.sec_catalog import CompanyTicker, import_sec_catalog

pytestmark = pytest.mark.database


@pytest.fixture(scope="module")
def dsn(migrated_url):
    value = migrated_url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(value) as connection:
        import_sec_catalog(connection, (
            CompanyTicker("0001046179", "TSM", "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"),
            CompanyTicker("0000006951", "AMAT", "APPLIED MATERIALS INC /DE"),
            CompanyTicker("0000099999", "OTHR", "TSMC Other Holdings"),
        ))
    return value


def aliases_of(dsn, entity_id):
    with psycopg.connect(dsn) as connection:
        return {row[0] for row in connection.execute(
            "SELECT alias_norm FROM entity_aliases WHERE entity_id = %s", (entity_id,)
        ).fetchall()}


def test_sec_import_adds_the_alias_without_the_state_tag(dsn):
    assert {"appliedmaterialsde", "appliedmaterials"} <= aliases_of(dsn, "ent_sec_0000006951")


def test_entities_resolve_only_by_identifier_or_id(dsn):
    with psycopg.connect(dsn) as connection:
        assert resolve_entity(connection, "TSM") == "ent_sec_0001046179"
        assert resolve_entity(connection, "1046179") == "ent_sec_0001046179"
        assert resolve_entity(connection, "ent_sec_0001046179") == "ent_sec_0001046179"
        with pytest.raises(LookupError):
            resolve_entity(connection, "Taiwan Semiconductor")  # names never resolve here


def test_reviewed_aliases_load_once(dsn):
    entry = AliasEntry("TSM", ("Taiwan Semi " + uuid4().hex[:6],), "r", False)
    with psycopg.connect(dsn) as connection:
        first = apply_aliases(connection, [entry])
        again = apply_aliases(connection, [entry])
    assert (first.added, again.added, again.present) == (1, 0, 1)


def test_an_alias_owned_by_another_entity_is_refused_and_nothing_is_written(dsn):
    fresh = "Fresh " + uuid4().hex[:6]
    entries = [AliasEntry("TSM", (fresh,), "r", False),
               AliasEntry("TSM", ("TSMC Other Holdings",), "r", False)]
    with psycopg.connect(dsn) as connection, pytest.raises(AliasFileError, match="already"):
        apply_aliases(connection, entries)
    assert fresh.lower().replace(" ", "") not in aliases_of(dsn, "ent_sec_0001046179")


def test_a_shared_alias_is_allowed_when_declared(dsn):
    with psycopg.connect(dsn) as connection:
        report = apply_aliases(connection, [AliasEntry("TSM", ("TSMC Other Holdings",), "r",
                                                       True)])
    assert report.added == 1
    assert "tsmcotherholdings" in aliases_of(dsn, "ent_sec_0001046179")


def test_an_unknown_entity_fails_the_whole_file(dsn):
    with psycopg.connect(dsn) as connection, pytest.raises(AliasFileError, match="resolve"):
        apply_aliases(connection, [AliasEntry("NOPE", ("Nope",), "r", False)])
