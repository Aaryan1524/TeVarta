# Te Varta decisions

Record consequential decisions without rewriting history. Use one concise entry per decision:

```text
YYYY-MM-DD — Decision — Reason — Owner(s)
```

The frozen MVP remains the source of truth. This log records implementation choices and evidence-driven changes made through the agreed review process; never place secrets here.

2026-09-12 — Source backbone — Select 15 primary/company and five wire/secondary candidates; activate none until T19 validates endpoint, robots/feed terms, and body-processing/storage rights — Aaryan; revisit per source in T19.
2026-09-12 — Initial curation targets — Use the documented 30-entity launch proxy — Aaryan; reorder after 10 waitlist responses or 25 live seeds.
2026-09-12 — Embeddings — Use `text-embedding-3-small` at 1,024 dimensions — Aaryan; revisit after the labelled clustering benchmark.
2026-09-12 — Relation defaults — Propose the 14 defaults in `docs/T01_EXECUTION_INPUTS.md` — Both founders confirm by 2026-09-16; revisit from Week 8 feedback fitting.
2026-09-12 — SC1 probe rate — Propose 1-in-6 through Week 8; require a sample-size review for 1-in-10 — Both founders confirm by 2026-09-16; implement in T40.
2026-09-12 — SC2 demo path — Require evidence-backed Taiwan and Netherlands edges separately; do not invent ASML → Taiwan — Both founders confirm by 2026-09-16; enforce in T15.
2026-09-12 — SC3 security routes — Propose refresh, sign-out, and account-deletion routes in T03 — Aaditya drafts; founders approve by 2026-09-16.
2026-09-12 — SC4 portfolio schema — Propose weighted `user_seeds` as the normalized beta holding layer — Both founders confirm by 2026-09-16; revisit with broker integration after beta.

2026-09-13 — SC3 security routes — Approve `POST /v1/auth/refresh`, `POST /v1/auth/sign-out`, and `DELETE /v1/account` for the v1 contract — Aaditya and Aaryan; required by §20.1 session revocation and deletion controls.

2026-09-13 — T05 persistence IDs — Keep internal UUIDs and unique API-facing public IDs for users/devices/articles/events, with text entity IDs — Aaryan; preserves §5.1 UUID merge references and T03 fixtures; adapters resolve IDs at the API boundary.
2026-09-13 — T05 schema scope — Implement frozen weighted seeds, claim evidence and invocation usage storage; do not equate implementation with SC4 approval or durable budget enforcement — Aaryan; SC4 still needs founder confirmation before final portfolio sign-off.
