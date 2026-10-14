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

2026-09-29 — T14 curated evidence format — Replace §6.2's single `evidence_url` with a required `evidence` list of `{url, source_type, publisher_group, locator?}`; reject curator-supplied `confidence` — Aaryan; §9.1 confidence needs source tier and publisher-group independence, which one URL cannot express. Both founders confirm at the Friday review before T15 batches merge.
2026-09-29 — T14 review records — Keep accept/edit/reject records in append-only `graph/curation/review_log.jsonl`, not a new Postgres table, and load only edges whose content SHA has an accepting record — Aaryan; records must be visible in the two-person PR and identical in every environment. Revisit if review volume outgrows PR review.
2026-09-29 — T14 drafting provider — Draft candidates through the OpenAI Responses API with strict structured output; the model ID and token prices come only from environment configuration, with no default — Aaryan; matches the existing OpenAI embeddings choice without fixing a model. Revisit when the `compile_event` provider is chosen (T30).

2026-10-08 — Proposed T25/T23 handoff (pending founder review) — Persist one shared expansion request per entity and read worker-prepared per-root graph views from PostgreSQL; T23 owns consumption, retries, and publication — Aaditya proposes, Aaryan owns T23; see `docs/T25_ONBOARDING_APIS.md`. No Celery dispatch or completed expansion is claimed by T25's independent slice.
2026-10-08 — T25 frozen-contract conflicts (unresolved) — Database entity types and seed source `onboarding` exceed v1's candidate/source enums. Return explicit unsupported-type/source errors instead of guessing or changing the contract — Aaditya flags; both founders must decide the taxonomy/source representation before full T25 closure.

2026-10-13 — T20 search-only publishers (pending founder confirmation) — A search result whose host matches no configured source domain enters `sources` as `web:<host>` with its own publisher group, `tier_value` 0.25 (§9.3 aggregator), inactive — Aaryan; unvetted publishers must not inflate corroboration or confidence. Reclassify a host by adding it to `config/sources.yaml`.
2026-10-13 — T20 metadata tagging guard (pending founder confirmation) — Names match only as proper-noun spans; there is no common-word lexicon, so sentence-initial or title-cased common words equal to a company alias are tagged — Aaryan; measured by the T22 tier-3 audit and `resolution_misses`; revisit with Week 10 resolution data (§6.4).

2026-10-13 — T23 impact direction (pending founder confirmation) — Expansion and impact propagation walk upstream only, using `graph_store.IMPACT_DIRECTION`: DEPENDS_ON, MANUFACTURES_AT, EXPOSED_TO, OPERATES_IN, REGULATED_BY, IN_INDUSTRY, PRODUCES and CUSTOMER_OF carry impact from target to source; SUPPLIES, SOLE_SOURCE_FOR and OPERATED_BY from source to target; SUBSIDIARY_OF, LISTED_AS and COMPETES_WITH both ways — Aaryan; keeps co-customers out of a seed's world (§4, §6.2). Consequence: holders of a supplier (TSMC, ASML) get no customer-side demand news. CUSTOMER_OF is read as supply exposure, not demand linkage.
2026-10-13 — T23 hop-2 bound reading (pending founder confirmation) — §6.3 "hop 2 only for nodes with weight ≥ 0.7" means the weight of the edge into the hop-1 node; hop 3 requires a hop-2 path score ≥ 0.15; seed weight 1.0 during global expansion — Aaryan; the per-user seed weight applies at fanout (T38).
2026-10-13 — T23 view types beyond v1 (pending, part of the T25 enum conflict) — Prepared views write v1 API types where they exist and the lowercased database type otherwise (`industry`, `macrofactor`) — Aaryan; GraphNode.entity_type is free text in v1, so this needs no contract change; revisit with the enum decision.

2026-10-13 — T19 search provider (pending Aaditya's confirmation) — Brave News Search is the one SearchProvider: metadata only, `freshness` window plus a per-result cut-off, behind the existing hard monthly cap (1,500 queries, alarm at 80%); results enter as unvetted `web:<host>` sources unless the host is configured — Aaryan chose it 2026-10-13; §2 hybrid discovery and §18 search budget. Revisit if result quality or cost is poor in Week 5 dogfooding.
=======
=======
2026-10-13 — T15 agent-drafted batches (pending founder confirmation) — Batches drafted in a Claude Code session are labelled `drafted_by: llm:claude-opus-5-5` with a session-generated `usage_request_id` that has no `usage_records` row, because the drafting ran outside the product's metered OpenAI path. Every excerpt is fetched from its source URL and verified verbatim before the batch is written — Aaryan; keeps model provenance truthful without inventing metering. Human acceptance (Aaryan) and Aaditya's PR review remain required for every edge.


