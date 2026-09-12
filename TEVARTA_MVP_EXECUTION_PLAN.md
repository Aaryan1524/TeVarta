# Te Varta MVP — Engineering Execution Plan

**Source:** `TEVARTA_MVP_BUILD_v2.1.md` (FROZEN)  
**Target beta:** 8 November 2026  
**Founders:** Aaryan (backend, graph, ingest, pipeline, scoring, cost) and Aaditya (iOS, API surface, auth, notifications, analytics, beta operations)

This is an execution plan for the frozen MVP, not a redesign. Aaryan and Aaditya use trunk-based development, short-lived branches, small PRs, same-day review, CI on every PR, fixture-first API development, and dated decisions in `decisions.md`.

## 1. Dependency Map

```text
Sources, curation targets, embedding model, relation weights
  -> API contract + fixtures (frozen Day 3)
  -> Postgres schema + component contracts
      -> SEC catalog -> entity resolution -> metadata tagging/tiering
      -> temporal/provenanced Neo4j operations -> reviewed curated edges
      -> feeds + capped entity sweep -> conditional fetch/dedupe/embed
      -> labelled articles -> clustering -> activation -> compile -> merge
      -> bounded impact traversal -> reverse-index fanout -> scored feed rows
      -> cached template-first why -> API -> iOS feed
      -> percentile thresholds -> capped push + local digest
      -> feedback + graph/probe measurement + cost reporting
      -> dogfood -> security/recovery/soak -> external beta -> Day-14 decision
```

Two chains run in parallel:

1. **World and event chain:** curated graph -> ingest -> clustering/merge -> impact -> relevance -> delivery.
2. **Client and evidence chain:** API fixtures -> SwiftUI -> auth/settings/APNs -> telemetry -> dashboard -> beta operations.

The chains join at stored, personal feed rows. API reads must stay cache/DB reads: no request-time LLM, search, or graph traversal.

## 2. Critical Path

1. Freeze contracts, schema, identity, scoring, and graph-write boundaries by Day 3.
2. Complete the 200 reviewed curated edges and real tiered ingest by the end of Week 2.
3. Pass clustering purity on the labelled 200-article set; support activation, compile, and merge in Week 3.
4. Produce bounded graph paths, fanout, relevance scores, and cached explanations in Week 4.
5. Complete thresholds, capped notifications, digest, backfills, and measurement by Week 5.
6. Pass recovery, security, p95 latency, and seven unattended days in Week 6.
7. Admit the cohort on 26 October, then collect the complete 14-day evidence window for the 8 November decision.

**Schedule protection:** Week 3 is the highest slip risk. Start the labelled set during Week 2. If clustering needs the specified fallback, use entity-plus-time-window grouping and evaluate it on the same set. If Week 3 slips, omit the Week 6 template-versus-LLM `why` comparison before reducing beta scope.

## 3. Development Phases

### PHASE 1 — Foundations and frozen interfaces

- **Phase Goal:** Establish the contracts, persistence, identity, evidence model, and deterministic primitives that let Aaryan and Aaditya work independently.

#### SUB-PHASE 1.1 — Execution inputs and interfaces

- **Sub-phase Goal:** Resolve the frozen document's Week 1 inputs and freeze the boundaries that otherwise block parallel work.

##### TASK T01 — Record execution inputs and decisions **CRITICAL PATH**

- **What must be implemented:** Select the 15–20 sources with `publisher_group`, `tier_value`, and reliability; choose the first 30 curation targets, embedding model, and initial relation weights. Record decision deadlines and specification conflicts below.
- **Owner:** Shared; Aaryan prepares graph/source inputs.
- **Dependencies:** None.
- **Deliverable:** Source configuration, curation target list, and dated `decisions.md` entries.
- **Verification / Done means:** Every source has all required fields; all inputs affecting schema/contracts are resolved or explicitly escalated before Day 3.
- **Can run in parallel with:** T02.
- **Relevant section(s) of the MVP document:** §§7.1, 9, 13, 15, 21.

##### TASK T02 — Create repository, CI, and shared fixture structure

- **What must be implemented:** Establish FastAPI, Celery, SwiftUI, migrations, tests, `contracts/`, fixtures, and `graph/curated_edges.yaml`; enable CI and dependency scanning.
- **Owner:** Aaditya.
- **Dependencies:** None.
- **Deliverable:** Runnable repository skeleton and PR workflows.
- **Verification / Done means:** Clean checkout runs backend smoke checks and iOS build; an intentionally failing check blocks merge; no secrets are committed.
- **Can run in parallel with:** T01, T05.
- **Relevant section(s) of the MVP document:** §§2, 13–15, 20.1.

##### TASK T03 — Freeze the API contract and mock server by Day 3 **CRITICAL PATH**

- **What must be implemented:** Define every §11 endpoint, request/response/error model, IDs, cursor behavior, authorization behavior, refresh/sign-out/deletion routes once SC3 is decided, and fixtures for success, empty, loading, ambiguity, expired auth, and merged events.
- **Owner:** Shared; Aaditya authors the contract/mock implementation.
- **Dependencies:** T01, T02.
- **Deliverable:** Versioned API contract, fixture JSON, and mock endpoints.
- **Verification / Done means:** Both founders approve it by 16 September; every endpoint has fixtures before backend implementation; Swift decoding and backend contract validation use the same JSON.
- **Can run in parallel with:** T05–T10.
- **Relevant section(s) of the MVP document:** §§11, 13–15, 20.1.

##### TASK T04 — Freeze useful component contracts

- **What must be implemented:** Define minimal contracts for `SearchProvider`, `EventCompiler`, named graph operations, scoring functions, graph expansion/status, prepared graph reads, feed writes, and APNs payloads. Include event revisions, evidence references, merge effects, and usage records.
- **Owner:** Shared; Aaryan authors worker/graph contracts.
- **Dependencies:** T01.
- **Deliverable:** Typed contracts and representative fixtures.
- **Verification / Done means:** Fixtures cover discovered article, ambiguous entity, compiled event, temporal edge, winning path, merged event, and notification. No provider framework or fallback implementation is added.
- **Can run in parallel with:** T03, T05.
- **Relevant section(s) of the MVP document:** §§2–5, 6.3, 8–12, 16.

#### SUB-PHASE 1.2 — Data, security, and canonical identity

- **Sub-phase Goal:** Make the data model, entity identity, score math, and graph writes reliable before curation and ingest.

##### TASK T05 — Implement all Postgres schema and indexes **CRITICAL PATH**

- **What must be implemented:** Migrate all §5.1 tables/indexes: composite alias key, identifier uniqueness, reverse seed index, vector columns, merged events, why cache, seven relevance components, feedback, and evidence/usage data.
- **Owner:** Aaryan.
- **Dependencies:** T01; SC4 must be resolved before final portfolio representation closes.
- **Deliverable:** Versioned migrations and database fixtures.
- **Verification / Done means:** Empty migration succeeds; aliases may map to multiple entities while identifiers remain one-to-one; duplicate seeds/feed rows fail; vector size matches the selected model.
- **Can run in parallel with:** T03, T06, T13.
- **Relevant section(s) of the MVP document:** §§1, 5.1, 9.4, 12, 16, 20.1.

##### TASK T06 — Establish secure environments and cost/usage baseline

- **What must be implemented:** Configure separate dev/prod credentials/databases, managed secrets, TLS/HSTS, encryption-at-rest, private R2, backups, log redaction, Sentry scrubbing, and per-component usage records/limits.
- **Owner:** Aaditya.
- **Dependencies:** T01, T02.
- **Deliverable:** Environment configuration, redaction tests, backup schedules, and initial usage reporting.
- **Verification / Done means:** Logs/Sentry omit APNs tokens, access tokens, and `apple_sub`; dev cannot use prod credentials; a synthetic limit breach is alerted without secret leakage; search has a hard-cap mechanism.
- **Can run in parallel with:** T05, T07–T10.
- **Relevant section(s) of the MVP document:** §§2, 15, 18, 20.1.

##### TASK T07 — Import the SEC catalog

- **What must be implemented:** Repeatably import `company_tickers.json` into catalog entities and identifier aliases, preserving canonical IDs and catalog/live separation.
- **Owner:** Aaryan.
- **Dependencies:** T05.
- **Deliverable:** Importer and import report.
- **Verification / Done means:** Reimport is idempotent; CIK/ticker/name fixtures resolve to stable IDs; catalog import alone does not create live graph expansion.
- **Can run in parallel with:** T11–T13.
- **Relevant section(s) of the MVP document:** §§5.1, 6.1, 6.4.

##### TASK T08 — Implement entity resolution rungs 1–3 **CRITICAL PATH**

- **What must be implemented:** Resolve exact identifiers, normalized exact aliases, and exchange-qualified tickers; disambiguate by co-occurrence then `seed_count`; log unresolved ambiguity without tagging it.
- **Owner:** Aaryan.
- **Dependencies:** T07, T04.
- **Deliverable:** Resolver, `resolution_misses` records, and fixtures.
- **Verification / Done means:** CIK, ticker, exchange-qualified ticker, and five SanDisk spellings resolve as expected; Apple/Mercury return candidates; unresolved ties log a miss and tag no entity.
- **Can run in parallel with:** T11–T13.
- **Relevant section(s) of the MVP document:** §§5.1, 6.4, 14.

##### TASK T09 — Implement deterministic confidence, importance, and path-score primitives **CRITICAL PATH**

- **What must be implemented:** Implement §9 confidence, source independence, severity values, importance, freshness, hop factors, and relevance; compute but assign zero weight to P/S/N/I.
- **Owner:** Aaryan.
- **Dependencies:** T04.
- **Deliverable:** Pure scoring module and numerical golden fixtures.
- **Verification / Done means:** §9 examples reproduce their stated results; syndicated domains count by publisher group; blogs cannot manufacture primary-source importance; hop factors are exactly 1/.90/.75/.55; no LLM output supplies confidence.
- **Can run in parallel with:** T11–T13.
- **Relevant section(s) of the MVP document:** §§6.2–6.3, 9.0–9.5.

##### TASK T10 — Implement temporal Neo4j write operations **CRITICAL PATH**

- **What must be implemented:** Add named, parameterized operations for entity/edge writes with relation allowlisting, mandatory provenance, confidence, validity, observed time, and invalidation.
- **Owner:** Aaryan.
- **Dependencies:** T05, T04, T09.
- **Deliverable:** One graph operations module and integration fixtures.
- **Verification / Done means:** Edges without provenance fail; revisions close the old valid interval and create a new relationship; historical reads preserve prior truth; no graph node has `user_id`; arbitrary or LLM-generated Cypher is impossible.
- **Can run in parallel with:** T11–T13.
- **Relevant section(s) of the MVP document:** §§1.1–1.2, 5.2, 6.2.

#### SUB-PHASE 1.3 — Auth, instrumentation, and fixture client

- **Sub-phase Goal:** Let Aaditya build and test the full app against stable responses while secure API identity lands.

##### TASK T11 — Implement Apple auth and session lifecycle

- **What must be implemented:** Validate Apple identity tokens server-side; issue ≤1-hour access tokens; implement refresh and sign-out revocation; add the common authorization dependency and auth rate limits.
- **Owner:** Aaditya.
- **Dependencies:** T03, T05, T06.
- **Deliverable:** Auth routes, sessions, and authenticated client flow.
- **Verification / Done means:** Invalid signatures, audience, expired tokens, and forged `apple_sub` fail; refresh succeeds; sign-out revokes; a second user cannot access the first user’s resource.
- **Can run in parallel with:** T07–T10.
- **Relevant section(s) of the MVP document:** §§11, 14, 20.1.

##### TASK T12 — Implement telemetry envelope and feedback endpoint

- **What must be implemented:** Define and persist all §16 events with action context, `slot_type`, `slot_position`, and session duration. Apply authorization, rate limits, and retry dedupe.
- **Owner:** Aaditya.
- **Dependencies:** T03, T05, T06.
- **Deliverable:** Client analytics interface, `/v1/feedback`, and fixtures.
- **Verification / Done means:** Every required event validates; duplicate delivery does not double count; zero-action impressions remain measurable; unauthorized attribution fails.
- **Can run in parallel with:** T08–T10.
- **Relevant section(s) of the MVP document:** §§5.1, 11, 16–17, 20.1.

##### TASK T13 — Build all SwiftUI shells against fixtures

- **What must be implemented:** Use existing Stitch designs for onboarding, graph construction, feed, detail, graph, entity detail, and settings; establish navigation/API client.
- **Owner:** Aaditya.
- **Dependencies:** T02, T03.
- **Deliverable:** Runnable SwiftUI app using the mock server.
- **Verification / Done means:** Every screen renders fixture success/loading/empty/error/ambiguity states; fixture event navigation works on simulator or device.
- **Can run in parallel with:** T07–T10.
- **Relevant section(s) of the MVP document:** §§11–14.

**Phase completion:** Repository has contracts, fixtures, migrations, catalog/resolution, scoring, graph writes, auth, telemetry, and iOS shells. Integration proof: iOS signs in to FastAPI/Postgres and renders frozen fixture models. Founder demo: SanDisk/Apple resolution, fixture event, and temporal edge evidence.

### PHASE 2 — Curated graph substance and real ingest

- **Phase Goal:** Build the 200-edge dependency asset and run real news through the required metadata-first pipeline.

#### SUB-PHASE 2.1 — Curated dependency edge workflow

- **Sub-phase Goal:** Make edge curation auditable, reviewed, temporal, and loadable as engineering work.

##### TASK T14 — Implement curation validation, drafting, and loader **CRITICAL PATH**

- **What must be implemented:** Validate YAML, resolve endpoints, retain evidence URLs/validity dates, compute confidence, load with `curated:<sha>`, and meter LLM candidate drafts. Persist accept/edit/reject review records.
- **Owner:** Aaryan; Aaditya provides second review for accepted edges.
- **Dependencies:** T08–T10, T06.
- **Deliverable:** YAML schema, drafting command, review records, idempotent loader.
- **Verification / Done means:** Missing evidence/review dates fail; unreviewed proposals cannot load; SHA resolves to reviewed source; rejected drafts are logged; reimport creates no duplicate edge.
- **Can run in parallel with:** T19–T20.
- **Relevant section(s) of the MVP document:** §§5.2, 6.2, 12, 15, 18.

##### TASK T15 — Load curated edge batches to 200 **CRITICAL PATH**

- **What must be implemented:** Review and load four 50-edge PR batches across the selected top 30 entities: supply dependencies, sole sources, geographic concentration, commodities/input, and regulatory exposure in the frozen priority order.
- **Owner:** Aaryan; Aaditya reviews every accepted PR.
- **Dependencies:** T14.
- **Deliverable:** 200 reviewed edges plus a coverage report.
- **Verification / Done means:** All 200 have valid endpoints, real evidence URLs, provenance, computed confidence, temporal fields, and review dates; intended evidence-backed dependency demonstrations work.
- **Can run in parallel with:** T19–T27.
- **Relevant section(s) of the MVP document:** §§6.2, 14–15, 19.

#### SUB-PHASE 2.2 — Hybrid discovery and three-tier ingest

- **Sub-phase Goal:** Discover broadly but only spend on bodies and embeddings according to the frozen policy.

##### TASK T19 — Implement feeds, one search adapter, and hard query budget

- **What must be implemented:** Poll configured sources every five minutes; implement the one permitted `SearchProvider`; schedule the daily top-50 seeded-entity sweep and include on-demand expansion search in the same hard monthly budget.
- **Owner:** Aaditya (temporary crossover to protect Aaryan’s curation capacity).
- **Dependencies:** T01, T04, T06.
- **Deliverable:** Metadata discovery jobs and usage/budget controls.
- **Verification / Done means:** Both paths emit the same candidate shape; daily search uses the last 24 hours; concurrent jobs cannot exceed the cap; the 80% alarm fires.
- **Can run in parallel with:** T14–T15, T20.
- **Relevant section(s) of the MVP document:** §§2–3, 7.1–7.2, 18–19.

##### TASK T20 — Implement URL dedupe, metadata tagging, and tier gate **CRITICAL PATH**

- **What must be implemented:** Deduplicate normalized URLs, resolve title/snippet mentions, and assign tiers from live/catalog status across all entity types.
- **Owner:** Aaryan.
- **Dependencies:** T05, T08, T04, T19.
- **Deliverable:** Deterministic metadata ingest stage.
- **Verification / Done means:** Duplicate URLs process once; live Taiwan is tier 1; catalog-only is tier 2; no match is tier 3; no body request occurs before tiering.
- **Can run in parallel with:** T21, T25–T27.
- **Relevant section(s) of the MVP document:** §§3, 7.2–7.3.

##### TASK T21 — Implement conditional fetch, body dedupe, tagging, and embeddings **CRITICAL PATH**

- **What must be implemented:** Fetch authorized bodies to private R2; SimHash dedupe; body tag; create tier-1 body and tier-2 title/snippet embeddings; meter fetch/storage/embedding use.
- **Owner:** Aaryan.
- **Dependencies:** T20, T06.
- **Deliverable:** Body-processing stage.
- **Verification / Done means:** Tier 1/2/3 receive exactly their defined treatment; duplicate bodies avoid repeated expensive processing while retaining provenance; embedding dimension and usage limits pass.
- **Can run in parallel with:** T25–T27.
- **Relevant section(s) of the MVP document:** §§3, 5.1, 7.2–7.4, 18.

##### TASK T22 — Implement quarantine, high-tier override, and 5% recall audit **CRITICAL PATH**

- **What must be implemented:** Retain tier-2 quarantine for 30 days and tier-3 metadata for 48 hours; always fetch `tier_value >= .9`; sample 5% of tier 3 and persist original tier/body discoveries; increment demand.
- **Owner:** Aaryan.
- **Dependencies:** T21.
- **Deliverable:** Quarantine lifecycle, recall sampler, and audit records.
- **Verification / Done means:** TTLs pass controlled-clock tests; high-tier fetch ignores initial tier; sampling is reproducible; recall audit preserves original assignment.
- **Can run in parallel with:** T25–T27.
- **Relevant section(s) of the MVP document:** §§3, 7.2–7.3, 17, 19.

#### SUB-PHASE 2.3 — Demand-driven expansion and onboarding

- **Sub-phase Goal:** Make a first seed create one reusable, bounded live subgraph.

##### TASK T23 — Implement first-seed expansion **CRITICAL PATH**

- **What must be implemented:** Promote catalog entities to live, load applicable curated edges, add specified EDGAR relationships, run targeted 30-day search, enforce 3-hop/path-score limits, and expose construction status/prepared graph view.
- **Owner:** Aaryan.
- **Dependencies:** T08, T10, T14, T19.
- **Deliverable:** `expand_entity` worker and status/view outputs.
- **Verification / Done means:** Second seed reuses the live graph; hop 2 requires `.7` weight, hop 3 requires `.15` path score, and traversal stops at 3 hops or `<.05`; concurrent first seeds produce one expansion.
- **Can run in parallel with:** T25–T26.
- **Relevant section(s) of the MVP document:** §§1.1, 3, 6.3, 9.2.

##### TASK T24 — Pre-warm approximately 50 likely seed entities

- **What must be implemented:** Promote likely early seeds using the same expansion machinery and approved evidence.
- **Owner:** Aaryan.
- **Dependencies:** T15, T23.
- **Deliverable:** Idempotent pre-warm command and coverage report.
- **Verification / Done means:** About 50 expected seeds are live with bounded/provenanced paths; gaps are reported instead of fabricated.
- **Can run in parallel with:** T25–T27.
- **Relevant section(s) of the MVP document:** §§6.1–6.3, 14.

##### TASK T25 — Implement entity search, seed, status, graph, and entity APIs

- **What must be implemented:** Back catalog autocomplete, seed creation/removal, construction status, graph/entity reads, seed-count updates, and expansion enqueueing.
- **Owner:** Aaditya.
- **Dependencies:** T03, T05, T08, T11–T12, T23.
- **Deliverable:** Authenticated onboarding/graph APIs.
- **Verification / Done means:** Seed creation/removal is idempotent and authorized; ambiguity returns candidates; graph read has no request-time traversal; cross-user reads/writes fail.
- **Can run in parallel with:** T20–T24 using fixtures until live integration.
- **Relevant section(s) of the MVP document:** §§1.3, 2, 6, 11, 16, 20.1.

##### TASK T26 — Connect onboarding, construction, and graph screens

- **What must be implemented:** Complete tap/type selection, ambiguity selection, construction status, graph/entity detail, removal, and associated analytics.
- **Owner:** Aaditya.
- **Dependencies:** T13, T12, T25.
- **Deliverable:** Working onboarding-to-graph flow.
- **Verification / Done means:** Real user selects an entity, observes construction, opens graph/detail, removes it, and produces valid analytics records.
- **Can run in parallel with:** T20–T24 against fixtures.
- **Relevant section(s) of the MVP document:** §§6.4, 11–14, 16.

#### SUB-PHASE 2.4 — Event benchmark preparation

- **Sub-phase Goal:** Give Week 3 a reviewed, reproducible quality benchmark.

##### TASK T27 — Assemble and label 200 articles **CRITICAL PATH**

- **What must be implemented:** Collect representative filings, following wires, syndication, concurrent stories, ambiguous entities, and stale-document examples; label event membership and define purity/fragmentation calculations.
- **Owner:** Aaditya coordinates; Shared resolves label disagreements.
- **Dependencies:** T19, T21.
- **Deliverable:** Versioned labelled dataset and annotation guide.
- **Verification / Done means:** 200 articles have reviewed labels and resolved disputes; the benchmark is reproducible at the start of Week 3.
- **Can run in parallel with:** T22–T26.
- **Relevant section(s) of the MVP document:** §§8, 14, 19.

**Phase completion:** Repo has curation tooling and 200 edges; real discovery/tiering/quarantine; expansion/pre-warm; real onboarding; labelled set. Integration proof: real article reaches correct tier, and real seed produces a shared live graph. Founder demo: trace curated dependency evidence and inspect an upstream Taiwan/Company article’s tier.

### PHASE 3 — Reliable event identity

- **Phase Goal:** Create one canonical, compiled event from related articles without duplicate user-visible cards.

#### SUB-PHASE 3.1 — Cluster and activate

- **Sub-phase Goal:** Establish measured event identity with deterministic lifecycle transitions.

##### TASK T28 — Implement and evaluate event clustering **CRITICAL PATH**

- **What must be implemented:** Search candidates in 72 hours sharing an entity; apply cosine/Jaccard thresholds; attach/update centroid or form event; tune both thresholds against T27.
- **Owner:** Aaryan.
- **Dependencies:** T21, T27.
- **Deliverable:** Clusterer and evaluation report.
- **Verification / Done means:** All 200 articles are replayed deterministically; purity is `>= .85`; fragmentation is reported; concurrent and stale cases are inspected.
- **Can run in parallel with:** T32, T34.
- **Relevant section(s) of the MVP document:** §§8, 14, 17, 19.

##### TASK T29 — Implement primary and ordinary activation **CRITICAL PATH**

- **What must be implemented:** Immediate activation at primary-source strength `.9`; otherwise two articles or 30 minutes; make transition idempotent.
- **Owner:** Aaryan.
- **Dependencies:** T28, T09.
- **Deliverable:** Activation transition and timed job.
- **Verification / Done means:** All three branches pass clock tests; an 8-K activates under a minute; concurrent jobs create one transition.
- **Can run in parallel with:** T32, T34.
- **Relevant section(s) of the MVP document:** §§3, 8.1, 14.

#### SUB-PHASE 3.2 — Compile and merge

- **Sub-phase Goal:** Compile once per event and preserve canonical identity when evidence later converges.

##### TASK T30 — Implement the structured EventCompiler **CRITICAL PATH**

- **What must be implemented:** Implement one strict JSON, temperature-zero compile call; persist summary, claims, entities, and queued proposals; only recompile for material changes.
- **Owner:** Aaryan.
- **Dependencies:** T29, T04, T06, T08.
- **Deliverable:** Compiler schema/prompt, validation, persistence, and cost/revision records.
- **Verification / Done means:** Summary is neutral and `<=60` words; severity is one frozen class/value; claims reference source articles; proposals cannot auto-commit; replay does not repeat a successful compile.
- **Can run in parallel with:** T32, T34.
- **Relevant section(s) of the MVP document:** §§3, 8.2, 9.0, 12, 18.

##### TASK T31 — Implement event merge **CRITICAL PATH**

- **What must be implemented:** On activation and nightly, merge active 72-hour pairs meeting `.88` cosine and `.50` entity overlap; keep earliest event, union evidence/entities, mark loser, trigger one recompile, invalidate caches, and rerender feed rows.
- **Owner:** Aaryan.
- **Dependencies:** T28–T30, T04.
- **Deliverable:** Idempotent merge operation and activation hook.
- **Verification / Done means:** Filing-plus-wire replay yields one canonical event; losing IDs resolve to the winner; no duplicate evidence/calls/cycles occur under replay or concurrency.
- **Can run in parallel with:** T32, T34, T35.
- **Relevant section(s) of the MVP document:** §§5.1, 8.2, 14, 19.

##### TASK T32 — Implement feed/event-detail APIs and merged-event handling

- **What must be implemented:** Implement cached/persisted feed pagination and event reads, including merged IDs; enforce authorization and preserve no-request-time-work rule.
- **Owner:** Aaditya.
- **Dependencies:** T03, T05, T11, T31.
- **Deliverable:** Live feed/detail API handlers and tests.
- **Verification / Done means:** Pagination is stable and duplicate-free; merged ID returns canonical event; handler test fails if it invokes LLM/search/traversal.
- **Can run in parallel with:** T28–T31.
- **Relevant section(s) of the MVP document:** §§2, 5.1, 11, 14, 20.1.

##### TASK T33 — Apply merge effects to cards and explanations **CRITICAL PATH**

- **What must be implemented:** Consolidate user feed rows and invalidate stale `why_cache` rows after a merge so users receive an update, not a second card.
- **Owner:** Aaditya; Aaryan reviews pipeline semantics.
- **Dependencies:** T31, T32.
- **Deliverable:** Merge consumer and regression test.
- **Verification / Done means:** One user/event card remains; stale explanation disappears; repeated merge is harmless.
- **Can run in parallel with:** T35.
- **Relevant section(s) of the MVP document:** §§5.1, 8.2, 11.

##### TASK T34 — Complete feed, detail, reader link-out, and event feedback

- **What must be implemented:** Connect feed, refresh, empty states, event detail, publisher links, impressions/clicks/saves/dismisses/not-relevant actions.
- **Owner:** Aaditya.
- **Dependencies:** T12–T13, T32.
- **Deliverable:** Instrumented real event-reading flow.
- **Verification / Done means:** Real compiled event opens through API; displayed position accompanies actions; source body text is never rendered; merged event remains usable.
- **Can run in parallel with:** T28–T31.
- **Relevant section(s) of the MVP document:** §§7.4, 11, 14, 16, 20.

##### TASK T35 — Add the Week 3 curation increment

- **What must be implemented:** Add approximately 25 reviewed dependencies prioritized by live `seed_count` and surfaced gaps.
- **Owner:** Aaryan; Aaditya reviews.
- **Dependencies:** T15.
- **Deliverable:** Approximately 225 accepted edges and coverage audit.
- **Verification / Done means:** New edges meet the same provenance/temporal/evidence checks and audit gaps are visible.
- **Can run in parallel with:** T32–T34.
- **Relevant section(s) of the MVP document:** §§6.2, 15, 19.

**Phase completion:** Repo has clustering report, activation, compiler, merge, real event APIs/UI. Integration proof: 8-K -> compile -> later wire stories -> merge -> one canonical response/card. Founder demo reports purity and shows a card update.

### PHASE 4 — Impact, relevance, and instrumented delivery surfaces

- **Phase Goal:** Deliver the right real event to the right user with a defensible dependency path.

#### SUB-PHASE 4.1 — Evidence to bounded impact

- **Sub-phase Goal:** Turn compiled entities and reviewed temporal edges into deterministic affected entities.

##### TASK T36 — Connect event evidence to deterministic scores and reviewed proposals

- **What must be implemented:** Resolve compiled entities, score confidence/importance, and route human-accepted proposals through graph writes.
- **Owner:** Aaryan.
- **Dependencies:** T30, T09–T10, T14.
- **Deliverable:** Post-compile score/accepted-edge integration.
- **Verification / Done means:** Identical evidence scores identically; syndication does not inflate independence; queued proposals do not alter the graph.
- **Can run in parallel with:** T40–T42.
- **Relevant section(s) of the MVP document:** §§5.2, 8–9, 12.

##### TASK T37 — Implement bounded impact propagation **CRITICAL PATH**

- **What must be implemented:** Traverse valid dependency paths in the agreed impact directions, including direct matches, using frozen hop/path limits and edge evidence.
- **Owner:** Aaryan.
- **Dependencies:** T36, T10, T23, T15.
- **Deliverable:** `propagate_impact` and golden scenarios.
- **Verification / Done means:** Taiwan event reaches expected dependent seeds and excludes unrelated/expired/four-hop/below-floor paths; result includes evidence/minimum path confidence.
- **Can run in parallel with:** T40–T42.
- **Relevant section(s) of the MVP document:** §§3–6, 9.1–9.2.

#### SUB-PHASE 4.2 — Fanout, relevance, and `why`

- **Sub-phase Goal:** Materialize personal results asynchronously and cheaply read them later.

##### TASK T38 — Implement reverse-index fanout and relevance writes **CRITICAL PATH**

- **What must be implemented:** Use `user_seeds(entity_id)` to find affected users; choose winning paths; score with the frozen relevance formula; persist all seven components, path JSON, and channel classification.
- **Owner:** Aaryan.
- **Dependencies:** T37, T09, T05.
- **Deliverable:** Fanout worker and idempotent `user_feed` writer.
- **Verification / Done means:** Golden users receive expected order/path; unrelated users receive none; replay duplicates no rows; adding users sharing a seed does not add compiler/search calls.
- **Can run in parallel with:** T40–T42.
- **Relevant section(s) of the MVP document:** §§1.3, 3, 5.1, 9.2–10.

##### TASK T39 — Implement template-first, cached `why` **CRITICAL PATH**

- **What must be implemented:** Render supported shapes from templates; cache by `(event_id, path_signature)`; use a metered LLM only for unsupported novel shapes.
- **Owner:** Aaryan.
- **Dependencies:** T38, T30, T04.
- **Deliverable:** Templates, cache, novel-shape renderer.
- **Verification / Done means:** Shared path users reuse one text; template paths make no LLM call; unsupported shape is rendered/cached once; merge invalidates stale text; no unsupported quantitative claim is emitted.
- **Can run in parallel with:** T40–T42.
- **Relevant section(s) of the MVP document:** §§3–4, 5.1, 8.2, 18.

##### TASK T40 — Implement probes, randomized positions, and shadow rank

- **What must be implemented:** Create keyword-only, recency-ordered seed candidates; inject observed probes at approved rate uniformly in positions 2–8; store shadow score/rank for each feed row.
- **Owner:** Aaditya.
- **Dependencies:** T30, T32, T38, T12; SC1 resolved.
- **Deliverable:** Probe injector, offline shadow ranker, placement tests.
- **Verification / Done means:** Probe makes no traversal/path scoring; never reaches slot 1; placement is uniform; canonical event does not duplicate; shadow data produces no imaginary impressions.
- **Can run in parallel with:** T39, T43.
- **Relevant section(s) of the MVP document:** §§5.1, 14, 16–17, 19.

#### SUB-PHASE 4.3 — Phone delivery and preferences

- **Sub-phase Goal:** Establish APNs and settings before applying the full policy.

##### TASK T41 — Implement encrypted APNs registration and phone handling

- **What must be implemented:** Encrypt stored tokens, send frozen event/digest payloads, handle deep links, and distinguish accepted transport from received/opened client events.
- **Owner:** Aaditya.
- **Dependencies:** T06, T11–T12, T34.
- **Deliverable:** Device endpoint, APNs adapter, iOS notification handling.
- **Verification / Done means:** Real phone receives/opens test notification in foreground/background/cold start; tokens are encrypted/redacted; receipt/open reaches telemetry.
- **Can run in parallel with:** T36–T39.
- **Relevant section(s) of the MVP document:** §§3, 5.1, 11, 14, 16, 20.1.

##### TASK T42 — Implement settings and local-time preferences

- **What must be implemented:** Complete profile GET/PATCH and iOS settings for timezone, threshold, and quiet hours, defaulting to 22:00–07:00.
- **Owner:** Aaditya.
- **Dependencies:** T11–T13.
- **Deliverable:** Persisted settings screen and profile API.
- **Verification / Done means:** Values survive relaunch; invalid/cross-user writes fail; timezone/DST fixtures give expected quiet interval; changes are instrumented.
- **Can run in parallel with:** T36–T39.
- **Relevant section(s) of the MVP document:** §§5.1, 10–11, 14, 16, 20.1.

##### TASK T43 — Add the Week 4 curation increment

- **What must be implemented:** Add approximately 25 reviewed edges based on seed demand and surfaced-event path gaps.
- **Owner:** Aaryan; Aaditya reviews.
- **Dependencies:** T35.
- **Deliverable:** Approximately 250 accepted edges and audit.
- **Verification / Done means:** All edges pass evidence/temporal validation; audit demonstrates whether surfaced paths used intended dependency edges.
- **Can run in parallel with:** T40–T42.
- **Relevant section(s) of the MVP document:** §§6.2, 14–15, 19.

**Phase completion:** Repo has impact/fanout/relevance, why cache, probes/shadow, APNs and settings. Integration proof: real event -> graph path -> stored personalized feed -> API -> iOS impression/action. Founder demo: each founder receives a different expected event/path; test APNs works.

### PHASE 5 — Delivery, lifecycle, and measurement

- **Phase Goal:** Make the product usable daily and make its outcomes measurable before external beta.

#### SUB-PHASE 5.1 — Thresholds, push, and digest

- **Sub-phase Goal:** Deliver selectively using observed distributions and hard safety rules.

##### TASK T44 — Implement nightly percentile recalibration **CRITICAL PATH**

- **What must be implemented:** Persist trailing-seven-day score percentiles, including logged-only candidates; use first-14-day P97≈70/P75≈40 bootstrap and absolute safety floors.
- **Owner:** Aaryan.
- **Dependencies:** T38.
- **Deliverable:** Recalibration job and policy snapshots.
- **Verification / Done means:** Controlled data reproduces percentiles; old samples drop from window; no push below 55 or `.6` path confidence.
- **Can run in parallel with:** T49–T50.
- **Relevant section(s) of the MVP document:** §§3, 5.1, 10, 14.

##### TASK T45 — Implement capped push selection/dispatch **CRITICAL PATH**

- **What must be implemented:** Select highest eligible scores; enforce percentile/floors, quiet hours, 30-minute cooldown, and two pushes/user/day; log outcomes.
- **Owner:** Aaryan; Aaditya verifies device behavior.
- **Dependencies:** T44, T41–T42, T39, T33.
- **Deliverable:** Notification policy worker and delivery log.
- **Verification / Done means:** Clock/concurrency tests cannot exceed cap or violate quiet/cooldown; below-confidence event never pushes; merge update never creates duplicate alert.
- **Can run in parallel with:** T49–T50.
- **Relevant section(s) of the MVP document:** §§8.2, 9.5, 10, 14, 19.

##### TASK T46 — Implement 07:00 local digest **CRITICAL PATH**

- **What must be implemented:** Schedule the default local digest of top five feed events independently of push eligibility and provide deep links/metrics.
- **Owner:** Aaryan; Aaditya completes iOS open behavior.
- **Dependencies:** T45, T38.
- **Deliverable:** Digest scheduler, payload, deep link.
- **Verification / Done means:** Timezone/DST fixtures yield one expected digest/day; content is correct top five; low push confidence does not remove a digest item; `digest_opened` persists.
- **Can run in parallel with:** T49–T50.
- **Relevant section(s) of the MVP document:** §§3, 10–11, 14, 16.

#### SUB-PHASE 5.2 — Backfill, maintenance, and deletion

- **Sub-phase Goal:** Handle first use, changing world evidence, and account removal correctly.

##### TASK T47 — Implement quarantine and seven-day cold-start backfill **CRITICAL PATH**

- **What must be implemented:** Promote matching 30-day quarantine records and run the separate seven-day article/event backfill on first seed; reuse global work and shared search budget.
- **Owner:** Aaryan.
- **Dependencies:** T23, T22, T33, T38–T39.
- **Deliverable:** Backfill workers/orchestration.
- **Verification / Done means:** New seed gets history; overlapping discovery/backfill creates no duplicate article/event/card; no new per-user compile call occurs.
- **Can run in parallel with:** T49–T50.
- **Relevant section(s) of the MVP document:** §§3, 6.3, 7.3, 12, 14.

##### TASK T48 — Implement nightly maintenance and demand reporting **CRITICAL PATH**

- **What must be implemented:** Schedule 03:00 decay/invalidation, nightly merge sweep, shadow rank, and demand report; refresh affected views, why cache, and feeds.
- **Owner:** Aaryan.
- **Dependencies:** T31, T37–T39, T22.
- **Deliverable:** Maintenance jobs and demand report.
- **Verification / Done means:** Controlled replay preserves graph history while removing stale current paths; merge sweep uses T31; demand reports reconcile with quarantine counts.
- **Can run in parallel with:** T49–T50.
- **Relevant section(s) of the MVP document:** §§2–3, 5.2, 7.3, 8.2, 14.

##### TASK T49 — Implement complete account deletion

- **What must be implemented:** Delete user-owned seeds, feed, feedback, devices, users, session credentials, and delivery/cache state without affecting the World Graph or other users.
- **Owner:** Aaditya.
- **Dependencies:** T11–T12, T25, T32, T41–T42.
- **Deliverable:** Deletion endpoint and settings action.
- **Verification / Done means:** Database assertions prove required deletions; in-flight workers cannot recreate the rows; authorization is revoked; shared graph remains unchanged.
- **Can run in parallel with:** T44–T48.
- **Relevant section(s) of the MVP document:** §§1, 11, 20–20.1.

#### SUB-PHASE 5.3 — Dogfood measurement and cost

- **Sub-phase Goal:** Make quality, behavior, and cost visible before external users.

##### TASK T50 — Implement the measurement dashboard

- **What must be implemented:** Report observed graph/probe utility, per-user direction/sign test, not-relevant, D7, push/digest opens, purity, and recall audit.
- **Owner:** Aaditya.
- **Dependencies:** T12, T40–T41, T45–T46.
- **Deliverable:** Lightweight dashboard and reproducible queries.
- **Verification / Done means:** Synthetic interactions produce exact expected totals; graph/probe denominators reconcile; shadow rank is excluded from the lift criterion; immature data is visible.
- **Can run in parallel with:** T47–T48, T51.
- **Relevant section(s) of the MVP document:** §§14, 16–17.

##### TASK T51 — Complete cost accounting and daily alerts

- **What must be implemented:** Report model calls/tokens, embeddings, searches, compute, database/Neo4j, Redis/R2, fixed spend, forecast, global/event cost, and marginal user cost; send redacted daily Telegram alert.
- **Owner:** Aaryan.
- **Dependencies:** T06, T19, T21, T30, T38–T39.
- **Deliverable:** Cost dashboard and alert.
- **Verification / Done means:** Extra users add no discovery/compiler work; per-event `<$0.02`, marginal additional user `<$0.05/month`, forecast `<$250/month`; each variable spend has a tested limit/alert.
- **Can run in parallel with:** T49–T50.
- **Relevant section(s) of the MVP document:** §§15, 17–19.

##### TASK T52 — Submit TestFlight and dogfood three days **CRITICAL PATH**

- **What must be implemented:** Submit the Week 5 build; both founders use feed/digest/push/feedback daily for three days and collect reproducible defects.
- **Owner:** Aaditya coordinates; Shared participates.
- **Dependencies:** T34, T39–T41, T45–T51.
- **Deliverable:** TestFlight build and dogfood observations.
- **Verification / Done means:** Both founders have three days actual use; all user-facing telemetry appears; release-blocking defects have a case and owner.
- **Can run in parallel with:** T53 and early recovery work.
- **Relevant section(s) of the MVP document:** §§14, 16, 20.

##### TASK T53 — Add the Week 5 curation increment

- **What must be implemented:** Add approximately 25 reviewed edges from actual seed demand and dogfood path gaps.
- **Owner:** Aaryan; Aaditya reviews.
- **Dependencies:** T43.
- **Deliverable:** Approximately 275 accepted edges and audit.
- **Verification / Done means:** Every new edge meets evidence/temporal requirements; corrections preserve history; audit connects findings to observed events.
- **Can run in parallel with:** T49–T52.
- **Relevant section(s) of the MVP document:** §§6.2, 14–15, 19.

**Phase completion:** Repo has thresholds, delivery policy, digest, backfills, maintenance, deletion, dashboards, and TestFlight build. Integration proof: signup -> history -> ranked feed -> digest/push -> phone action -> metrics/cost. Founder demo: three days of use reconciled to dashboards.

### PHASE 6 — Recovery, quality, and release verification

- **Phase Goal:** Prove safe independent operation before external access.

#### SUB-PHASE 6.1 — Recovery and security

- **Sub-phase Goal:** Test safeguards in failure instead of discovering them at launch.

##### TASK T54 — Implement retries, dead letters, and replay **CRITICAL PATH**

- **What must be implemented:** Add bounded retries/backoff, dead-letter handling, and inspect/replay procedures using Celery/Redis/Postgres.
- **Owner:** Aaryan.
- **Dependencies:** T45, T47–T48, T31, T51.
- **Deliverable:** Recovery behavior, fault tests, concise operations instructions.
- **Verification / Done means:** Fetch/model/database/APNs faults recover or become inspectable dead letters; replay preserves canonical identity, notification caps, and cost limits.
- **Can run in parallel with:** T55–T59.
- **Relevant section(s) of the MVP document:** §§14–15, 18, 20.1.

##### TASK T55 — Test Postgres and Neo4j restores

- **What must be implemented:** Restore automated backups to isolated environments and validate restored content.
- **Owner:** Aaryan.
- **Dependencies:** T05–T06, T10.
- **Deliverable:** Restore procedure and observed proof.
- **Verification / Done means:** Both databases restore; user ownership, event links, graph validity intervals, and evidence references survive.
- **Can run in parallel with:** T56–T61.
- **Relevant section(s) of the MVP document:** §20.1.

##### TASK T56 — Sign off §20.1 security checklist **CRITICAL PATH**

- **What must be implemented:** Verify every identity, secrets, data, and operations control in deployed release candidate.
- **Owner:** Shared; Aaditya assembles evidence.
- **Dependencies:** T55, T49, T11–T12, T06, T41.
- **Deliverable:** Signed checklist linked to tests/evidence.
- **Verification / Done means:** No checklist item is assumed; any exposure is rotated before sign-off.
- **Can run in parallel with:** T57–T61.
- **Relevant section(s) of the MVP document:** §§14, 20.1.

#### SUB-PHASE 6.2 — Quality and operational gates

- **Sub-phase Goal:** Correct dogfood defects with evidence and verify seven independent operating days.

##### TASK T57 — Apply dogfood-backed ranking, clustering, recall, and notification fixes **CRITICAL PATH**

- **What must be implemented:** Replay observed defects; fix implementation errors; make only jointly approved numerical adjustments supported by evidence; review recall and notification quality.
- **Owner:** Shared; Aaryan owns pipeline/scoring changes.
- **Dependencies:** T52, T28, T22, T44, T40.
- **Deliverable:** Focused fixes, regression cases, decisions.
- **Verification / Done means:** Weekly purity `>=.85`; recall miss rate `<10%`; representative direct/multi-hop results are approved; explanation/push defects are fixed.
- **Can run in parallel with:** T55–T56, T59–T61.
- **Relevant section(s) of the MVP document:** §§9–10, 14, 17, 19.

##### TASK T58 — Compare template and LLM `why`, if schedule permits

- **What must be implemented:** Run the frozen Week 6 comparison on matching path examples within existing cost controls.
- **Owner:** Shared.
- **Dependencies:** T39, T52.
- **Deliverable:** Result and recorded choice.
- **Verification / Done means:** Both variants use same examples; unsupported claims fail review; template wins ties. Omit this task if Week 3 slipped.
- **Can run in parallel with:** T60.
- **Relevant section(s) of the MVP document:** §§4, 14, 18.

##### TASK T59 — Complete the Week 6 curation increment

- **What must be implemented:** Add approximately 25 reviewed dependencies; audit all review dates and surfaced-event coverage.
- **Owner:** Aaryan; Aaditya reviews.
- **Dependencies:** T53.
- **Deliverable:** Approximately 300 accepted edges.
- **Verification / Done means:** Every edge has evidence/validity; overdue reviews/errors are explicitly dispositioned; loader suite passes.
- **Can run in parallel with:** T55–T58, T60–T61.
- **Relevant section(s) of the MVP document:** §§6.2, 14–15, 19.

##### TASK T60 — Prove seven unattended days and feed p95 **CRITICAL PATH**

- **What must be implemented:** Run integrated workload without manual rescue; record job completion, duplicate effects, delivery, latency, and spend.
- **Owner:** Aaryan.
- **Dependencies:** T54, T52, T45, T48.
- **Deliverable:** Seven-day operations/latency report.
- **Verification / Done means:** Seven consecutive days pass without manual recovery; feed p95 `<200ms`; digest/maintenance run; spend is within controls. Any release-blocking intervention restarts this gate.
- **Can run in parallel with:** T55–T59, T61.
- **Relevant section(s) of the MVP document:** §§2, 14, 17–18, 20.1.

##### TASK T61 — Prepare 25–30 external TestFlight participants

- **What must be implemented:** Recruit waitlist participants, exercise install/onboarding instructions, prepare support/interviews, and confirm TestFlight readiness.
- **Owner:** Aaditya.
- **Dependencies:** T52.
- **Deliverable:** Ready cohort and beta-operations checklist.
- **Verification / Done means:** 25–30 participants ready; instructions exercised; access remains gated on T56/T60.
- **Can run in parallel with:** T54–T60.
- **Relevant section(s) of the MVP document:** §§14, 19–20.

**Phase completion:** Repo has recovery procedures, restore proof, security sign-off, quality regressions, 300 edges, and soak report. Integration proof: a faulted pipeline replays without duplicate cards/notifications. Founder demo: restore, replay, then inspect seven days of operation.

### PHASE 7 — External beta and daily evidence

- **Phase Goal:** Place the completed instrumented product with 25–30 users without invalidating the experiment.

#### SUB-PHASE 7.1 — Operate cohort and verify measurement

- **Sub-phase Goal:** Keep product quality and observed data trustworthy through the beta window.

##### TASK T62 — Open external beta **CRITICAL PATH**

- **What must be implemented:** Grant TestFlight access after all gates pass and enable founder-approved probe configuration; Week 7 is 1-in-6.
- **Owner:** Shared; Aaditya coordinates.
- **Dependencies:** T56–T61, T40, T50.
- **Deliverable:** Active cohort and configuration record.
- **Verification / Done means:** Users authenticate, seed, read, and feedback successfully; probe placement/caps are active; 26 October entry preserves 14-day evidence window.
- **Can run in parallel with:** T63–T64 after entry.
- **Relevant section(s) of the MVP document:** §§14, 16–17, 20.1.

##### TASK T63 — Run daily pipeline/cost checks and weekly quality audit

- **What must be implemented:** Review failures, resolution misses, curation gaps, clusters, recall, notification quality, and cost; ship bug fixes and obvious ranking corrections only.
- **Owner:** Aaryan; Shared approves scoring changes.
- **Dependencies:** T62.
- **Deliverable:** Daily records, weekly labelled audit, focused corrections.
- **Verification / Done means:** Every day has a check; incidents have reproductions/dispositions; no deferred feature is introduced.
- **Can run in parallel with:** T64.
- **Relevant section(s) of the MVP document:** §§6.2, 14–19.

##### TASK T64 — Verify measurement integrity during real use

- **What must be implemented:** Reconcile cards, analytics, positions, exposures, action joins, and eligible retention cohorts; repair missing events.
- **Owner:** Aaditya.
- **Dependencies:** T50, T62.
- **Deliverable:** Daily measurement-integrity report.
- **Verification / Done means:** Known sessions reconcile; no-action impressions remain; rate/position match configuration; D7 only uses mature cohorts; shadow rank remains offline.
- **Can run in parallel with:** T63.
- **Relevant section(s) of the MVP document:** §§14, 16–17.

**Phase completion:** Released build, daily operations, and verified observations. Integration proof: external user displayed slot -> action -> correct graph/probe utility. Founder demo: reconcile one real user journey end-to-end.

### PHASE 8 — Data readout and MVP decision

- **Phase Goal:** Decide with the frozen success/kill criteria, not a new architecture review.

#### SUB-PHASE 8.1 — Analyse and decide

- **Sub-phase Goal:** Combine observed slot outcomes, valid optional fitting, and interviews into a reproducible decision.

##### TASK T65 — Complete ten user interviews

- **What must be implemented:** Conduct ten interviews grounded in concrete feed, path, relevance, and notification examples.
- **Owner:** Aaditya.
- **Dependencies:** T62.
- **Deliverable:** Interview records and evidence synthesis.
- **Verification / Done means:** Ten users represented; claims distinguish anecdote from observed cohort metrics.
- **Can run in parallel with:** T66.
- **Relevant section(s) of the MVP document:** §§14, 17.

##### TASK T66 — Fit relevance weights only if qualifying feedback reaches 2,000

- **What must be implemented:** Use stored components and click-minus-dismiss feedback for specified logistic regression; retain shadow rank only for offline analysis.
- **Owner:** Aaryan; Shared owns any scoring change.
- **Dependencies:** T63–T64.
- **Deliverable:** Reproducible fitting report or explicit insufficient-data result.
- **Verification / Done means:** Sample count/labels reported; below 2,000, current weights remain; no deferred novelty/model-confidence feature is added.
- **Can run in parallel with:** T65.
- **Relevant section(s) of the MVP document:** §§9.4, 12–14, 16.

##### TASK T67 — Write the Day-14 decision memo **CRITICAL PATH**

- **What must be implemented:** Evaluate all §17 product/cost criteria from actual observed graph/probe slots and full beta window.
- **Owner:** Shared.
- **Dependencies:** T63–T66 and complete observation window.
- **Deliverable:** 8 November decision memo with reproducible queries/results.
- **Verification / Done means:** Every metric has value, denominator, window, and disposition; undefined/insufficient data is explicit; memo records proceed, rethink/kill, or evidence-limited decision.
- **Can run in parallel with:** Analysis preparation during T65–T66.
- **Relevant section(s) of the MVP document:** §§14, 16–18.

**Phase completion:** Analysis queries, fitting result, interviews, and decision memo. Integration proof: decision metrics recompute from persisted production observations. Founder demo: reproduce graph/probe comparison and inspect underlying impressions.

## 4. Parallel Work Map

| Phase | Aaryan | Aaditya | Shared | Blocking dependency |
|---|---|---|---|---|
| 1 | Schema, catalog, resolver, scoring, graph writes | Repo/CI, environments, auth, telemetry, SwiftUI | Inputs and contracts | API contract by Day 3 |
| 2 | Curation, tiering, body processing, expansion | Discovery adapter, onboarding APIs/UI, benchmark coordination | Edge review and label disagreements | Bodies/labels needed for Week 3 |
| 3 | Clustering, activation, compiler, merge, curation | Feed/detail APIs/UI, merge card effects | Fallback decision if needed | Purity gate before scoring |
| 4 | Impact, fanout, relevance, `why`, curation | Probes, APNs, settings | Ranking/probe decisions | Stored feed rows join both chains |
| 5 | Thresholds, delivery, digest, backfill, maintenance, cost | Deletion, dashboard, TestFlight | Dogfood | Complete telemetry/delivery |
| 6 | Recovery, restore, soak, curation | Security evidence, participant readiness | Quality fixes, sign-off | Security + seven-day run |
| 7 | Pipeline/cost/quality checks | Support and telemetry reconciliation | Scoring/incident decisions | Measurement integrity |
| 8 | Weight fitting | Interviews | Decision memo | Full evidence window |

## 5. Integration Checkpoints

| Checkpoint | Deadline | Integration proof |
|---|---:|---|
| IC1 — API contract | Day 3 | Swift decoder and backend validator accept the same fixture JSON |
| IC2 — Identity | Week 1 | iOS ↔ auth API ↔ Postgres accepts real sign-in and rejects cross-user access |
| IC3 — Graph expansion | Week 2 | Seed ↔ expansion ↔ Neo4j ↔ graph view reuses live subgraph |
| IC4 — Real ingest | Week 2 | Feed/search ↔ tiering ↔ Postgres/R2 applies exact body/embed policy |
| IC5 — Event identity | Week 3 | Ingest ↔ cluster ↔ compile ↔ merge produces one canonical event |
| IC6 — Merge effect | Week 3 | Merge ↔ feed/cache ↔ API/iOS updates an existing card |
| IC7 — Personal relevance | Week 4 | Events ↔ Neo4j ↔ fanout ↔ feed returns correct users and paths |
| IC8 — Observed measurement | Week 4 | Feed ↔ `why_cache` ↔ iOS ↔ feedback records exact displayed context |
| IC9 — Delivery | Week 5 | Notification worker ↔ APNs ↔ device applies caps/quiet/digest |
| IC10 — Lifecycle | Week 5 | Signup backfill and deletion are correct under worker races |
| IC11 — Recovery | Week 6 | Fault/retry/restore replays without duplicate effects |
| IC12 — Beta evidence | Weeks 7–8 | Real impressions/actions ↔ metrics reproduce §17 criteria |

## 6. Risk Gates and Specification Conflicts

### SPEC CONFLICT — requires founder decision

1. **Probe rate:** §14 says Week 8 may drop to 1-in-10; §16 says 1-in-6 for Weeks 7–8 and only then 1-in-10. Decide in T01/T40 and record it.
2. **Week 2 demo path:** §6.2 depicts ASML exposure to Netherlands and TSMC exposure to Taiwan, while §14 calls for Apple -> TSMC -> ASML -> Taiwan. Evidence must determine the actual curation path. Decide before T15.
3. **Security endpoints:** §20.1 requires refresh, sign-out revocation, and deletion, but §11 omits them. Add agreed minimal routes to T03.
4. **Normalized portfolio schema:** §12 requires retaining one, while §5.1 specifies weighted seeds only. Decide whether seeds satisfy it before T05; brokerage remains deferred.

| Risk | Cleared when | Required response if failed |
|---|---|---|
| Curated graph too thin/wrong | 200 reviewed edges by Week 2, ~300 by Week 6, provenance validation passes | Reallocate to curation/correction; do not replace with taxonomy |
| Resolution guesses | Ambiguity tests log/skip unresolved candidates | Correct rungs 1–3 only |
| Metadata recall too low | Audit has denominator and `<10%` live-entity misses | Widen deterministic tagger and replay; `>20%` is rethink gate |
| Clustering fails | 200-article purity `>=.85` | Use frozen entity/time fallback; test on same set |
| Fast path duplicates | Filing+wires gives one event/card | Repair merge before notifications |
| Bad notification policy | Caps, quiet hours, confidence floor, digest pass clocks/device | Correct policy before beta |
| Experiment invalid | Impressions/actions and probe positions reconcile | Repair instrumentation before cohort entry |
| Security/recovery failure | §20.1 signed, restore tested, 7-day run passes | Fix and rerun affected gate |
| Cost overrun | Metered limits/alerts; forecast `<$250` | Fix repeated work and enforce existing budgets |

The Day-14 criteria remain frozen: graph/probe mean utility `>=1.5x` to ship and `<=1.15x` to rethink; positive direction `>=65%` / `<50%`; median not-relevant `<10%` / `>25%`; D7 `>40%` / `<20%`; push-open `>25%` / `<10%`; weekly purity `>=.85` / `<.70`; tier-3 audit `<10%` / `>20%`.

## 7. First Seven Development Days

| Day | Aaryan | Aaditya | Shared end-of-day evidence |
|---|---|---|---|
| Day 1 — Sep 14 | Source classifications, top-30 targets, graph/scoring contract draft | Repo/CI, endpoint/fixture draft, environment/Apple setup | Record conflicts and capacity; source list complete |
| Day 2 — Sep 15 | Migrations, SEC importer, component fixtures | Mock endpoints, redaction/secrets, Swift fixtures | Confirm IDs, dimensions, required auth routes, telemetry fields |
| Day 3 — Sep 16 | Validate schema/catalog, freeze graph/scoring/compiler contracts | Freeze API/mock, Swift decoding, implementation begins | Approve T03/T04 and record contract version |
| Day 4 — Sep 17 | Scoring golden tests, temporal write module, resolver | Server-side Apple validation, sessions, authorization dependency | Demonstrate provenance and auth rejection |
| Day 5 — Sep 18 | Resolver/temporal tests, curation validator/loader | Auth integration, telemetry, instrumented fixture screens | Demo sign-in, SanDisk/Apple, fixture event, temporal edge |
| Day 6 — Sep 21 | Start first 50 curated edges; metadata tiering fixtures | Feed poller/search adapter, seed API implementation | Review first curation PR; verify budget accounting |
| Day 7 — Sep 22 | Next 50 edges; conditional fetch/dedupe/embedding | Onboarding states; collect/label benchmark articles | Demonstrate real article tier and correct body/embed scope |

## 8. GitHub Project Structure

Use one GitHub Project.

| Plan level | GitHub representation |
|---|---|
| Phase | Single-select `Phase` field: phases 1–8 |
| Sub-phase | Parent issue containing goal and phase acceptance links |
| Task | Sub-issue named with T01–T67 and its complete task template |

Keep fields limited to `Assignee`, `Status` (Todo / Ready / In progress / Review / Done), `Phase`, `Priority` (P0 critical path/release gate; P1 required; P2 only T58), native `Blocked by`, parent issue, and a `critical-path` label.

Use three views:

1. **Execution:** grouped by status, current phase, plus ready next-phase work.
2. **Parallel work:** grouped by assignee, showing phase, priority, and blockers.
3. **Phase acceptance:** sub-phase parent issues with task progress and demo/integration evidence.

Each task issue should use this body:

```markdown
Implement:
Owner:
Dependencies:
Deliverable:
Done means:
Can run in parallel with:
MVP sections:
```

Attach test output or a short device-demo record before closing a task. Link PRs to tasks and use native issue blockers rather than maintaining a second dependency tracker.
