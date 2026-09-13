"""Explicit PostgreSQL migration target; supports SQL preview and shared connections."""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url


def database_url():
    value = os.environ.get("TEVARTA_POSTGRES_URL")
    if not value:
        raise RuntimeError("Set TEVARTA_POSTGRES_URL explicitly before running migrations.")
    url = make_url(value)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("T05 migrations require PostgreSQL with pgvector.")
    return url.set(drivername="postgresql+psycopg")


def migrate(connection) -> None:
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=database_url(), literal_binds=True, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = context.config.attributes.get("connection")
    if supplied is not None:
        migrate(supplied)
        return
    engine = create_engine(database_url(), poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
