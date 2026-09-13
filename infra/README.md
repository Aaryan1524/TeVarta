# T06 secure-environment baseline

This directory defines reviewable desired state only. It contains no cloud
credentials, account IDs, endpoints, or secrets.

## Before production is enabled

1. Create separate managed-secret-store scopes named `development` and
   `production`. Set `TEVARTA_CREDENTIAL_SET` to the corresponding scope and
   inject database/R2 credentials only at runtime.
2. Provision separate Postgres and Neo4j databases and a private R2 bucket for
   each environment. Managed encryption at rest must be enabled for all three.
3. Terminate TLS at the API edge and verify that production sends HSTS. The API
   process redirects HTTP only when `TEVARTA_ENV=production`.
4. Configure Sentry with `app.operations.scrub_sentry_event` as `before_send`;
   its SDK DSN belongs only in the managed secret store.
5. Configure daily managed backups to match `backup-schedules.json`. Record a
   successful isolated restore before beta; T55 owns the restore execution.
6. Send the daily cost report through the approved alert channel. The message
   may contain aggregate usage and caps, never tokens, Apple subjects, URLs with
   credentials, or request bodies.

## Enforced locally

- `EnvironmentSettings` rejects a credential-set/environment mismatch and
  obvious cross-environment service references.
- `SensitiveDataRedactionFilter` and `scrub_sentry_event` redact APNs tokens,
  access tokens, Apple subjects, authorization values, request bodies, and
  cookies.
- `SearchUsageLedger` atomically reserves a query before dispatch, emits an 80%
  warning once, and rejects work once the configured cap is reached.

The persisted usage schema is deliberately deferred to T05. Until then, the
ledger supplies a deterministic process-local reporting boundary for task-level
tests and worker integration.
