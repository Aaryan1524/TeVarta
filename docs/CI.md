# CI setup and verification

The repository supplies three required CI workflows:

- **Backend contract:** lint, fixture/model validation, mock smoke tests, and a
  generated-OpenAPI drift check.
- **iOS:** shared-fixture Swift decoding and a simulator app build. macOS minutes
  bill at 10x Linux, so `scripts/ios_ci_changes.sh` skips the build unless `ios/`,
  `fixtures/v1/`, `contracts/v1/`, `Makefile`, or the workflow changed. The skip is
  job-level, so `iOS / build` reports "skipped" and still satisfies a required check.
  Run it manually from the Actions tab (`workflow_dispatch`) to force a build.
- **Database:** Alembic migrations and every PostgreSQL integration test
  (`make database-test`) against a disposable pgvector service.
- **Graph:** every `neo4j`-marked test (`make graph-test`) against a disposable
  `neo4j:5.26-community` service. Once a URI is set, the Neo4j fixtures connect or
  error and never skip, so a broken service fails the job. It can't pass empty. The
  job also runs a PostgreSQL service, so tests marked both `database` and `neo4j` (the
  Phase 2 integration proof) run here; the Database job skips them without Neo4j.
- **Dependency review:** dependency changes are reviewed on pull requests;
  Dependabot keeps Python and Actions dependencies current. On private repositories
  without GitHub Advanced Security, the Dependency Review action is configured to
  continue on error while Dependabot handles scheduled vulnerability scanning.

After the first push, configure `Backend contract / test`, `Backend contract /
required-check-guard`, `Database / migrations`, `Graph / neo4j`, and `iOS / build`
as required status checks for the
protected integration branch. GitHub branch-protection settings are account
state, so they cannot be represented or verified purely in this checkout.

To verify that the guard actually blocks a merge candidate, run this only in a
throwaway branch or CI job:

```sh
TEVARTA_CI_FORCE_FAILURE=1 ./scripts/verify_required_checks.sh
```

It exits non-zero by design. `backend/tests/test_ci_guard.py` proves that
behavior without making normal CI fail.
