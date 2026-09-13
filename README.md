# TeVarta

Te Varta surfaces news that affects the people, companies, places, and systems a user cares about, using a shared evidence-backed world graph and a small per-user overlay.

## Build sources

- [`TEVARTA_MVP_BUILD_v2.1.md`](TEVARTA_MVP_BUILD_v2.1.md) — frozen beta product, architecture, scope, and eight-week plan.
- [`TEVARTA_MVP_EXECUTION_PLAN.md`](TEVARTA_MVP_EXECUTION_PLAN.md) — implementation task breakdown and execution plan.
- [`AGENTS.md`](AGENTS.md) — repository-wide instructions for coding agents and reviewers.
- [`docs/BUILD_WORKFLOW.md`](docs/BUILD_WORKFLOW.md) — phase gates, task delivery, model-advisor reviews, commits, PRs, and handoff templates.
- [`decisions.md`](decisions.md) — append-only record of consequential implementation decisions.

## Local foundation checks

```sh
make backend-install
make backend-lint
make backend-test
make ios-test
make ios-build
```

The backend is a fixture-backed FastAPI mock for the draft v1 contract. It is
not a substitute for the real auth, data, graph, or worker implementations in
their assigned tasks. See [`contracts/v1/README.md`](contracts/v1/README.md)
for contract status and the required founder decision on SC3 security routes.

T05 adds the PostgreSQL schema independently of the mock API. See
[`migrations/README.md`](migrations/README.md) for database setup, SQL previews,
and `make database-test` against disposable PostgreSQL/pgvector databases.
