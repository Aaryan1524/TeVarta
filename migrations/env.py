"""Alembic environment placeholder owned by the T05 schema task."""

from alembic import context


def run_migrations_offline() -> None:
    raise RuntimeError("No schema migration exists yet; T05 owns the first migration.")


def run_migrations_online() -> None:
    raise RuntimeError("No schema migration exists yet; T05 owns the first migration.")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
