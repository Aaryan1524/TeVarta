# T26 — Onboarding, construction, and graph screens on the real API

**Owner:** Aaditya (implemented by Aaryan for the Phase 2 push)
**MVP:** §6.4 ambiguity, §11 endpoints 2, 3, 7, 8, §14 Week 2, §16 analytics

## Flow

1. **Search** (`GET /v1/entities/search`): tap or type. Ambiguous results show
   "We found more than one match" and the user picks one (§6.4).
2. **Add** (`POST /v1/seeds`, `{entity_id, weight: 1.0, source: "manual"}`):
   idempotent on the server, so retrying never double-adds.
3. **Construction** (`GET /v1/seeds/status`): polled every 2 s. `ready` reloads the
   graph and shows BEGIN; `failed` shows the server's detail with a retry. After
   about 2 minutes the user is told it will appear in Your world when ready, rather
   than spinning forever.
4. **Graph and entity detail** (`GET /v1/graph`, `GET /v1/entities/{id}`): every node
   opens its detail.
5. **Remove** (`DELETE /v1/seeds/{id}`): confirmation dialog, then the graph reloads.

## Auth

Every request carries the signed-in session's access token, refreshed first if it
has expired (`AuthenticationViewModel.accessToken()`). The Debug fixture preview
uses the mock server's fixture token. Debug builds may sign in against a local
`http://127.0.0.1` or `http://localhost` backend; Release requires https.

## Analytics

`seed_added` and `seed_removed` are recorded **server-side** by T25 in the same
transaction as the mutation (`seed_mutation:` idempotency keys). The client does not
send its own copies, so seed events are never counted twice (see the #35 note for
T50).

## Contract gap (for the Friday review)

v1 has no seeds list or `is_seed` flag, so the client cannot tell the user's own
seeds from entities reached through them. "Remove from my world" therefore appears
on every entity. Removing a reached-only entity is a server no-op, and it stays
visible, as the dialog explains. A `GET /v1/seeds` endpoint or an `is_seed` field
would fix this; it needs both founders.

## Verification

- Debug and Release simulator builds succeed; `swift test` 5 passed.
- In the simulator against the fixture-mode mock: the app's first request
  (`GET /v1/feed`) went out with the token provider's bearer and returned 200.
- **Needs a human:** tap through search → add → construction → BEGIN → entity →
  remove in the simulator. The real-user path (sign-in against a backend with auth
  and Postgres, then seed rows and server `seed_*` receipts) needs Apple Developer
  signing (Phase 1's IC2).
