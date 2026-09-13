# Te Varta API contract — v1 draft

**Status:** Drafted by Aaditya for joint founder approval by 2026-09-16. The
OpenAPI artifact is generated from the Pydantic boundary models in
`backend/app/contracts.py`; run `PYTHONPATH=backend python3 scripts/export_openapi.py`
after an intentional model change. The test suite fails if the committed artifact
does not match the running mock.

## Compatibility rules

- All IDs are opaque strings. Clients may display/store them but must not infer
  meaning from their prefixes.
- Timestamps are RFC 3339 UTC instants. `cursor` is opaque, returned as
  `next_cursor`, and is valid only for its feed query.
- `GET /v1/feed` defaults to `limit=20`; accepted range is 1–50. A missing
  `next_cursor` means there is no subsequent page.
- All user-owned routes require `Authorization: Bearer <access-token>`. The
  mock recognizes `fixture-access-token`; production enforcement belongs to T11.
- Errors use `{ "error": { "code", "message", "request_id", "details" } }`.
  Clients should branch on `code`, not message text.
- `fixtures/v1/scenario_manifest.json` maps every mock route to a response
  fixture. Its explicit `loading` fixture is a client state served only with
  `GET /v1/feed?scenario=loading` for UI development.
- A merged event is returned as its canonical event with `requested_event_id`
  preserved and the losing IDs in `merged_from`; clients replace/update the
  existing card instead of showing a second card.
- Article responses expose publisher, title, URL, and timestamps only—never a
  source body.

## Proposed SC3 additions requiring approval

The frozen MVP requires refresh, sign-out revocation, and account deletion but
does not list routes for them in §11. This draft proposes:

| Method | Route | Behavior |
|---|---|---|
| `POST` | `/v1/auth/refresh` | Exchange a refresh token for a ≤1-hour access session. |
| `POST` | `/v1/auth/sign-out` | Revoke the authenticated session; return `204`. |
| `DELETE` | `/v1/account` | Schedule deletion of user-owned data; return `202`. |

They remain proposals until both founders record approval in `decisions.md`.
