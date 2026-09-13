# Database migrations

T02 establishes the Alembic boundary only. T05 owns the first PostgreSQL schema
migration and must not be bypassed with ad-hoc `create_all` calls.

Run migrations only against an explicitly selected environment once T05 lands.
