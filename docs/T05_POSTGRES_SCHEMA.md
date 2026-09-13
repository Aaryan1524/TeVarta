# T05 — Postgres foundation

Owner: Aaryan. Branch: `aaryan/phase-1/t05-postgres-schema`.

Outcome: runnable, versioned PostgreSQL migrations for the entire frozen §5.1
schema, including required indexes, evidence/usage storage and synthetic tests.

Dependencies: merged T01 input packet, T02 Alembic scaffold and T06 operations
baseline. SC4 remains a founder decision; its absence does not block implementing
the explicitly frozen weighted-seed table, but prevents final portfolio sign-off.

Done means: an empty PostgreSQL database upgrades successfully; names can resolve
to multiple entities while identifiers stay unique; duplicate seeds/feed rows
fail; both embeddings enforce 1,024 dimensions; all seven scoring measurements
persist; user deletion preserves shared evidence; rollback/re-upgrade works.

Verification: `make database-test` against PostgreSQL 16/pgvector 0.8.6, backend
pytest, Ruff on backend/migrations, and generated OpenAPI diff. The added CI job
repeats the database tests against a disposable service. See
[`migrations/README.md`](../migrations/README.md) for commands and boundaries.

This task adds persistence structure. It does not replace the API mock, implement
authentication, execute production migrations, or record Aaditya's approval.

Local verification on 2026-09-13: 80 backend tests passed, including 46 database
tests; Ruff passed and OpenAPI remained unchanged. Schema review found no
blocking issue. Shadow measurements are now mandatory and infinite session
durations are rejected, with regression coverage for both.
