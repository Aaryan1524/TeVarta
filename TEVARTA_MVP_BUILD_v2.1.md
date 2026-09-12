# Te Vārtā — MVP Build Document

**Version:** 2.1 (FROZEN) · **Date:** 12 Sep 2026 · **Target beta:** 8 Nov 2026
**Team:** 2 engineers · **Budget:** < $250/month through beta

Shared source of truth for the beta. If code and this doc disagree, one of them
is wrong — fix it the same day.

> **This document is frozen.** The next legitimate reason to change it is
> evidence from dogfooding in Week 5 — a threshold that is visibly wrong, a
> cluster that is visibly bad, a `why` string that reads badly on a real phone.
> Not another review pass. Three rounds found real bugs; a fourth will mostly
> find preferences.

---

## What Te Vārtā is

News that affects your life reaches you, instead of you going to find it.
The king's messenger, not the doomscroll.

A user states what they care about. We surface the ~10 things a day that
actually touch it, explain *why they touch it*, and suppress the other ~2,800.

Two rules govern every decision below.

> **1. Expensive work happens once, globally. Personalization is a cheap lookup.**
> Cost per article is fixed. Cost per user is near zero.

> **2. LLM as compiler, not runtime — and not judge.**
> LLMs extract and phrase. They never own state, decide relevance, traverse the
> graph, set confidence, or write arbitrary queries.

Anything violating either rule is wrong regardless of how good the demo looks.

---

## Changelog

### v2.0 → v2.1 (this version)

Six corrections plus four consequences they implied. Two touch schema or worker
topology; the rest are formulas and constants.

| # | Change | Class |
|---|---|---|
| A | `entity_aliases` composite PK — one alias may map to several entities | **Schema** |
| B | Ingest reordered: tag on metadata → tier → fetch body only for tier 1 | **Topology** |
| C | Hop penalty is an explicit factor table; thresholds are percentiles, not guessed constants | Constants |
| D | Corroboration = `independence × avg_source_quality`, so three blogs ≠ one 8-K | Formula |
| E | `extraction_uncertainty` removed from confidence entirely | Formula |
| F | Primary-source fast path — an 8-K activates immediately, no 30-min wait | Condition |
| G | **Event merge** — required by F, which can otherwise create duplicate events | New component |
| H | Measurement: graph slots vs probe slots is the kill criterion; shadow ranking is offline only | Experiment design |
| I | Tier-2/3 recall guard — always fetch high-tier sources; 5% tier-3 audit sample | New component |
| J | Curation replanned: 200 edges in Week 2, ~25/week after, LLM-drafted + human-accepted | Plan |
| K | Compute line added to cost model; §20.1 security requirements added | Ops |

### v1.0 → v2.0

| # | Change |
|---|---|
| 1 | Gazetteer descoped — SEC ticker file only, deep-resolve on demand |
| 2 | Curated dependency edges as a first-class component |
| 3 | Hybrid discovery: push feeds + bounded entity search sweep |
| 4 | Three-tier ingest with quarantine, no hard drop |
| 5 | One structured LLM call per event |
| 6 | Confidence computed from observable evidence |
| 7 | Importance from primary-source strength, not article volume |
| 8 | Shadow ranking replaced the 50/50 A/B split |
| 9 | Cost measured as marginal-per-user and per-event |

---

## 0. How to use this doc

| Section | Read it when |
|---|---|
| 1–3 | Before writing any code |
| 4–10 | Building a backend component |
| 11 | Building iOS or changing an API response |
| 12 | Tempted to build something — check the deferred list first |
| 13–15 | Every Monday |
| 16–17 | Every Friday. This is how we know if it worked. |
| 18–21 | When something breaks or someone asks a hard question |

---

## 1. The three graphs

### 1.1 World Graph (shared, Neo4j)
The asset. Entities and relationships, with time and provenance. No `user_id`
appears in it. Ever.

Grows **on demand**: an entity enters when a user seeds it; expansion is
bounded. We are not ingesting the world — we ingest the union of what our users
care about, once. The second user to seed TSLA gets the subgraph free.

### 1.2 Evidence layer (Postgres + R2)
Every edge points back to what produced it. Nothing enters the World Graph
without provenance — including hand-curated edges (§6.2). This is what makes
the "why" defensible and the confidence computable.

### 1.3 User overlay (Postgres, tiny)
A user is a list of seeded entities with weights.

```
user_seeds:  user_id → entity_id, weight, source
```

100,000 users holding AAPL = 100,000 rows, not 100,000 graphs.

---

## 2. System architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        iOS APP (SwiftUI)                         │
│   onboarding · feed · event detail · your graph · settings       │
└─────────────────────────────┬────────────────────────────────────┘
                              │  HTTPS, JWT
┌─────────────────────────────▼────────────────────────────────────┐
│                    API (FastAPI, stateless)                      │
│   All reads are cache/DB reads. No LLM. No search. No traversal. │
│   p95 target < 200 ms                                            │
└──────┬────────────────────────────────────────────┬──────────────┘
       │ reads                                      │ writes
┌──────▼─────────────┐                    ┌─────────▼──────────────┐
│  Redis (feed cache)│                    │ Postgres (+ pgvector)  │
└────────────────────┘                    │ users, seeds, articles,│
                                          │ events, feed, feedback,│
                                          │ entity index + aliases │
                                          └─────────▲──────────────┘
                                                    │
══════════════ ASYNC — nothing above waits on this ════════════════
                                                    │
┌───────────────────────────────────────────────────┴──────────────┐
│                  WORKERS (Celery + Redis broker)                 │
│                                                                  │
│  DISCOVERY (metadata only)                                       │
│    poll_sources (15–20 feeds, 5 min)                             │
│    entity_sweep (top ~50 seeded entities, daily, capped)         │
│         │                                                        │
│    url_dedupe → tag_metadata → TIER (§7.3)                       │
│         │                                                        │
│         ├─ T1 → fetch_body → body_dedupe → tag_body → embed      │
│         ├─ T2 → quarantine (title+snippet embed)                 │
│         └─ T3 → metadata, 48h TTL  (+5% audit sample)            │
│         │                                                        │
│    cluster_events → merge_events (§8.2)                          │
│         │                                                        │
│    compile_event ◄──── ONE structured LLM call per event         │
│         │                                                        │
│    score_confidence · score_importance   (deterministic, §9)     │
│         │                                                        │
│    propagate_impact ──────────────────────────────► Neo4j        │
│         │                                                        │
│    fanout (reverse index) → score_and_write_feed → Postgres      │
│         │                                                        │
│    render_why (template; LLM only for novel path shapes)         │
│         │                                                        │
│    notify → APNs                                                 │
│                                                                  │
│  ON DEMAND  expand_entity · backfill_from_quarantine             │
│  NIGHTLY    decay_and_invalidate · merge_sweep · shadow_rank ·   │
│             recalibrate_thresholds · demand_report               │
└──────────────────────────────────────────────────────────────────┘
                                │
                   ┌────────────▼─────────────┐
                   │ Neo4j — World Graph      │
                   │ temporal · provenanced   │
                   └──────────────────────────┘
```

**Discovery is hybrid.** A push backbone of 15–20 curated feeds runs
continuously — free, reproducible, and it surfaces things nobody queried for,
which is where serendipity comes from. On top, a bounded daily search sweep over
the top ~50 seeded entities catches small-cap and niche coverage the fixed feeds
miss. Search is a supplement with a hard query budget, never the backbone.

---

## 3. Component spec

| # | Component | Det. / LLM | Runs | Cadence |
|---|---|---|---|---|
| 1 | `poll_sources` | Det. | per source | 5 min |
| 2 | `entity_sweep` | Det. planner + search API | top 50 entities | daily, budget-capped |
| 3 | `url_dedupe` | Det. | per candidate | inline |
| 4 | `tag_metadata` | Det. (alias match on title+snippet) | per candidate | inline |
| 5 | `tier_article` | Det. | per candidate | inline |
| 6 | `fetch_body` | Det. | tier 1 + high-tier sources + audit sample | inline |
| 7 | `body_dedupe` | Det. (SimHash) | per fetched body | inline |
| 8 | `tag_body` | Det. | per fetched body | inline |
| 9 | `embed` | Model, not LLM | tier 1 (body), tier 2 (title+snippet) | inline |
| 10 | `cluster_events` | Det. | per tier-1 article | inline |
| 11 | **`merge_events`** | Det. | per activation + nightly | §8.2 |
| 12 | **`compile_event`** | **LLM ×1** | per **event** | on activation (§8.1) |
| 13 | `score_confidence` | Det. | per edge/event | after compile |
| 14 | `score_importance` | Det. | per event | after compile |
| 15 | `propagate_impact` | Det. (graph) | per event | on event update |
| 16 | `fanout` | Det. (index) | per event | on event update |
| 17 | `score_and_write_feed` | Det. | per (event, user) | on event update |
| 18 | `render_why` | Template; LLM for novel path shapes | per **path signature** | cached |
| 19 | `notify` | Det. (percentile thresholds) | per user | on write + 07:00 digest |
| 20 | `expand_entity` | Det. | per new entity | on first seed |
| 21 | `backfill_from_quarantine` | Det. | per new entity | on first seed |
| 22 | `decay_and_invalidate` | Det. | global | nightly 03:00 |
| 23 | `recalibrate_thresholds` | Det. | global | nightly (§10) |
| 24 | `shadow_rank` | Det. | per event | offline analysis only (§16) |

**LLM calls/day at 50 users:** ~120 events × 1, plus a handful of novel-path
`why` renders. **Flat in user count.** That is the whole point.

---

## 4. Personalization — how "this affects you" works

```
Event          Fremont plant halts production
Event entities Tesla Fremont Factory, Tesla Inc
Your seed      TSLA
Path found     Fremont Factory —OPERATED_BY→ Tesla Inc —LISTED_AS→ TSLA
Rendered       "Affects your TSLA — Tesla operates the Fremont plant, which has
                halted production."       2 hops · confidence 0.94
```

The personalization is that **the path is yours**. Path computation is per
(event, user), deterministic, microseconds. The *text* is shared.

**Sharing key:** `(event_id, path_signature)` — the sequence of relation types
and entity IDs. 5,000 users holding TSLA share one path → one string generated,
5,000 deliveries. A user holding a Tesla supplier gets a different path and a
different string. Distinct path shapes per event: typically 1–5. That is the
generation count, not the user count.

**Template first:**

> `Affects your {SEED} — {SEED_NAME} {RELATION_PHRASE} {MID}, which {EVENT_PHRASE}.`

An LLM render fires only for path shapes the template can't express. A/B in
Week 6; if the template ties, keep the template.

**Copy discipline.** Never ship a quantitative claim the system can't back.
"Potential 14% deviation in shipping costs" is exactly the string that destroys
trust the first time it's wrong. Relationship + event + confidence. Nothing
invented.

---

## 5. Data model

### 5.1 Postgres

```sql
users(id, apple_sub, created_at, tz, notif_threshold, quiet_start, quiet_end)
devices(id, user_id, apns_token_enc, platform, updated_at)

-- the ONLY table linking users to the graph
user_seeds(user_id, entity_id, weight NUMERIC DEFAULT 1.0,
           source TEXT,          -- 'onboarding'|'manual'|'broker'
           created_at, PRIMARY KEY(user_id, entity_id))
CREATE INDEX ON user_seeds(entity_id);        -- reverse index. hottest path.

entities(entity_id TEXT PK, canonical_name, type, ticker, cik,
         status TEXT,           -- 'catalog' | 'live'   (§6.1)
         seed_count INT DEFAULT 0, created_at)

-- CHANGE A: one surface form may map to several entities.
-- "Apple" → Apple Inc. AND the fruit topic. "Mercury" → company, planet, element.
-- The resolver returns CANDIDATES and picks by context; it does not assume one.
entity_aliases(alias_norm TEXT, entity_id TEXT, source TEXT,
               confidence NUMERIC, is_identifier BOOL DEFAULT false,
               PRIMARY KEY(alias_norm, entity_id))
CREATE INDEX ON entity_aliases(alias_norm);
CREATE UNIQUE INDEX ON entity_aliases(alias_norm) WHERE is_identifier;
  -- identifiers (CIK, ticker, exchange-qualified symbol) stay 1:1.
  -- plain names do not.

sources(id, domain, feed_url, publisher_group TEXT, tier_value NUMERIC,
        reliability NUMERIC, poll_interval_s, active BOOL)
        -- publisher_group collapses syndication: 40 sites carrying one Reuters
        -- wire are ONE independent source. Load-bearing for §9.1 and §9.3.

articles(id, url_hash UNIQUE, url, source_id, title, snippet, published_at,
         fetched_at, simhash BIGINT, r2_key, lang, tier INT,
         embedding VECTOR(1024), embedding_scope TEXT)  -- 'body'|'title_snippet'|null
article_entities(article_id, entity_id, mentions INT, salience NUMERIC,
                 found_in TEXT,        -- 'metadata' | 'body'
                 PRIMARY KEY(article_id, entity_id))

quarantine(article_id PK, tagged_entity_ids TEXT[], expires_at)       -- §7.3
recall_audit(article_id, tier_assigned INT, entities_found_in_body TEXT[], ts)

events(id, canonical_title, event_type, severity_class TEXT, severity NUMERIC,
       importance NUMERIC, confidence NUMERIC, status,
       merged_into UUID NULL,        -- §8.2
       first_seen, last_update, article_count INT, centroid VECTOR(1024))
event_articles(event_id, article_id, PRIMARY KEY(event_id, article_id))
event_entities(event_id, entity_id, role, PRIMARY KEY(event_id, entity_id))
event_summaries(event_id PK, summary TEXT, model, generated_at)

why_cache(event_id, path_signature TEXT, text TEXT, method TEXT,
          PRIMARY KEY(event_id, path_signature))

user_feed(user_id, event_id, score NUMERIC, components JSONB,
          path_json JSONB, shadow_score NUMERIC, shadow_rank INT,
          slot_type TEXT,        -- 'graph' | 'keyword_probe'   (§16)
          slot_position INT,
          channel TEXT, created_at, PRIMARY KEY(user_id, event_id))
CREATE INDEX ON user_feed(user_id, score DESC, created_at DESC);

score_distribution(day DATE, p50 NUMERIC, p75 NUMERIC, p95 NUMERIC,
                   p97 NUMERIC, p99 NUMERIC)                          -- §10

notification_log(id, user_id, event_id, sent_at)
feedback(id, user_id, event_id, action, slot_type, slot_position, ts)
resolution_misses(id, surface_form, context, article_id, ts)
demand_log(entity_id, quarantined_article_count, week)
```

`components JSONB` stores **all seven** relevance terms though only three carry
weight (§9.5). Logged from day one so weights get fitted, not guessed.

### 5.2 Neo4j

```cypher
(:Entity {entity_id, canonical_name, type})
```

Types: `Company, Security, Product, Facility, Commodity, Industry, Country,
Person, Regulation, MacroFactor, Topic, Organization`

Relations — **dependency relations matter most**, listed first:

```
DEPENDS_ON        SUPPLIES         SOLE_SOURCE_FOR    MANUFACTURES_AT
EXPOSED_TO        CUSTOMER_OF      COMPETES_WITH      PRODUCES
SUBSIDIARY_OF     OPERATED_BY      OPERATES_IN        REGULATED_BY
LISTED_AS         IN_INDUSTRY
```

Edge properties — every edge, no exceptions:

```cypher
{
  weight        : 0.0-1.0,   -- relation importance
  confidence    : 0.0-1.0,   -- COMPUTED (§9.1). Never LLM self-reported.
  valid_from    : datetime,  -- when it became true in the world
  valid_until   : datetime,  -- null = still true
  observed_at   : datetime,  -- when WE learned it
  invalidated_at: datetime,
  source_ids    : [text],    -- article ids or curated:<sha>. NEVER empty.
  evidence_count: int,
  directness    : 0.0-1.0,   -- 1.0 stated, 0.6 inferred from co-mention
  origin        : 'curated' | 'structured' | 'extracted'
}
```

**Bi-temporal from day one.** Never overwrite. Set `valid_until`, write a new
edge. Retrofitting this is a rewrite, which is why it is not deferrable.

**All writes go through named parameterized Cypher functions in one module.**
No LLM-generated Cypher, ever.

---

## 6. Building the graph

### 6.1 Catalog vs live

**Catalog** — `SEC company_tickers.json`, one file, ~10,000 US-listed companies
with CIK, ticker, name. Powers onboarding autocomplete and identifier
resolution. Nothing else. One afternoon.

**Live** — entities that actually entered the graph because a user seeded them
or expansion reached them. Deeply resolved: aliases, edges, provenance. Expect
**100–300** through the beta.

Out of scope for Week 1 as disproportionate to a 25-user beta: bulk Wikidata,
GLEIF, Exhibit 21 subsidiary trees, Form 4 officers, 13F institutions. A 13F
filer is not why a user cares about a factory fire.

### 6.2 Curated dependency edges — the highest-value work in the project

**The problem.** EDGAR and Wikidata reliably give
`Apple —IN_INDUSTRY→ Technology`. Taxonomy. What the product runs on is:

```
Apple —DEPENDS_ON→ TSMC —DEPENDS_ON→ ASML —EXPOSED_TO→ Netherlands
                     └──EXPOSED_TO→ Taiwan
```

No free structured source produces that chain. Ship without it and Week 8 says
"graph shows no lift" — a true statement about a graph that isn't Te Vārtā.

**CHANGE J — revised plan.** The v2.0 target of 500 edges in two days was wrong.
At 5–8 minutes per edge with evidence lookup, weight judgment, YAML, and review,
500 edges is ~54 hours — 2.7 weeks of Person A's total capacity, which would
have silently consumed Week 2.

| When | Target | Scope |
|---|---|---|
| Week 2 | **200 edges** | top 30 entities, hand-curated |
| Weeks 3–6 | **~25/week** | prioritized by live `seed_count` — curate what users actually hold |
| Beta end | ~300 edges | all reviewed, all provenanced |

**Drafting shortcut:** use an LLM to *propose* candidate edges from 10-K Item 1A
and supplier disclosures; a human accepts, edits, or rejects each one. That is
drafting-with-review, not auto-commit, so it stays inside Rule 2 and cuts
per-edge time roughly 3×. Rejected candidates get logged — they are training
data for later automation.

**Format** — `graph/curated_edges.yaml`, version-controlled, reviewed in PRs:

```yaml
- source: TSMC
  relation: DEPENDS_ON
  target: ASML
  weight: 0.90
  directness: 1.0
  rationale: "Sole commercial supplier of EUV lithography systems"
  evidence_url: "https://www.asml.com/en/investors/annual-report"
  curated_by: aaryan
  curated_at: 2026-09-22
  review_by: 2027-03-22
```

**Non-negotiable:** curated edges carry the same provenance, confidence, and
validity as extracted ones — `origin: 'curated'`, `source_ids:
['curated:<sha>']`, a real `evidence_url`, a `review_by` date. Manual must never
mean unprovenanced. The moment curated edges are exempt from the evidence rules,
the graph stops being auditable and §9.1 silently breaks.

Curation priority order:
1. Supply-chain dependencies for the top 30 held companies
2. Single points of failure (`SOLE_SOURCE_FOR`) — the highest-signal edges you own
3. Geographic concentration (`MANUFACTURES_AT`, `EXPOSED_TO`)
4. Commodity and input dependencies
5. Regulatory exposure

### 6.3 Expansion on new seed

```
new seed: SNDK
  → resolve via catalog (identifier → exact alias → ticker normalization)
  → already live?  link user, STOP.              ← free reuse across users
  → else promote to live:
      • curated edges touching it, if any
      • EDGAR: SIC industry, direct filer relationships
      • one targeted search sweep, last 30d
      • hop 2 only for nodes with weight ≥ 0.7; hop 3 only if path score ≥ 0.15
      • stop at 3 hops or path score < 0.05
  → backfill_from_quarantine for this entity (§7.3)
  → 7-day article backfill
```

**Pre-warm before launch:** promote the ~50 most-likely-seeded entities to live
and curate their edges before the first signup. Onboarding is then instant for
most first seeds instead of holding on screen 3.

### 6.4 Entity resolution ladder (v1 = rungs 1–3)

1. Exact identifier — CIK / ticker / exchange-qualified symbol
2. Exact alias match on normalized string → **may return multiple candidates**
3. Ticker-with-exchange normalization (`2330.TW`, `NYSE: TSM` → same node)

**Candidate disambiguation (change A):** when rung 2 returns >1 entity, pick by
(a) co-occurrence with other entities already tagged in the same article, then
(b) `seed_count` as a prior. If still ambiguous, tag none and log to
`resolution_misses`. Never guess silently.

Rungs 4–6 (fuzzy, embedding, LLM) deferred to Week 10. **Log every miss** so
Week 10 works from data.

---

## 7. Ingest

### 7.1 Discovery — hybrid

**Push backbone: 15–20 sources.** Bias hard toward primary: SEC full-text feed,
Federal Register, exchange notices, central bank releases, company IR feeds for
the top held names, then 4–6 wires and quality secondaries.

**Entity sweep: daily, budget-capped.** Top ~50 entities by `seed_count`, one
query each, last 24h. ~50 queries/day covers the long tail the fixed feeds miss.
Hard monthly cap enforced in code, alarm at 80%.

### 7.2 Article flow — CHANGE B

v2.0 had `fetch → dedupe → tag → tier`, which contradicted tier 2's "no fetch of
body." You cannot decide not to fetch after fetching. Correct order:

```
DISCOVERY RESULT (metadata only: title, url, snippet, publisher, timestamp)
        │
   url_hash exact dedupe
        │
   tag on title + snippet          ← lightweight, no network
        │
   TIER DECISION  (§7.3)
   ┌────┴─────────────┬──────────────────┐
  T1                 T2                 T3
   │                  │                  │
 fetch body      store metadata     metadata only
   │             + tags             48h TTL
 body simhash    + title/snippet    no embedding
   dedupe          embedding             │
   │              quarantine 30d    5% audit sample
 tag body         demand_log++       → fetch + tag
 (may add                              → recall_audit
  entities)
   │
 body embedding
   │
 cluster
```

**Override — always fetch, regardless of tier:** any article from a source with
`tier_value ≥ 0.9` (SEC, regulators, exchanges, company IR). Low volume, high
value, and these are exactly the articles that trigger the fast path in §8.1.

**CHANGE I — recall guard.** Tagging on metadata alone misses an article that
names SNDK only in paragraph six. That article becomes tier 3 and you never find
out. So: **sample 5% of tier 3**, fetch and fully tag it, write the result to
`recall_audit`. Review weekly. This is how you measure what your own filter is
costing you instead of assuming it costs nothing. If the audit shows >10% of
tier-3 articles contain live entities, the metadata tagger needs widening.

**Embedding policy (resolving the v2.0 contradiction):**

| Tier | Embedding |
|---|---|
| 1 | full body |
| 2 | title + snippet |
| 3 | none |

### 7.3 Three-tier gate — no hard drop

| Tier | Condition | Treatment |
|---|---|---|
| **1** | Touches a **live** entity of any type — including Country, Commodity, Industry, Regulation, MacroFactor, Topic | Full pipeline |
| **2** | Touches a **catalog** entity but nothing live | Quarantine 30 days. `demand_log++`. |
| **3** | No match | Metadata, 48h TTL, then discarded. 5% audited. |

What this buys:

1. *"Taiwan hit by major earthquake"* is **tier 1**, because Taiwan is a live
   Country node the moment anyone seeds TSMC. Upstream events with no company
   name are caught — this is the core capability.
2. `demand_log` is a free demand signal — which entities to pre-warm next,
   before a user asks.
3. `backfill_from_quarantine`: when a user seeds a new entity, its quarantined
   articles from the last 30 days become tier 1 retroactively. Their feed has
   history on day one.

### 7.4 Content rights

Full text stored privately in R2 for processing only. The app shows title, our
own summary, and a link out — **never source body text**. Respect `robots.txt`
and feed terms.

---

## 8. Event clustering

Hardest engineering problem in the MVP. Budget an extra week.

```
for each new tier-1 article A:
  candidates = events where last_update > now-72h
               AND shares ≥1 entity with A
  for each C:
      sim  = cosine(A.embedding, C.centroid)
      jacc = |ents(A) ∩ ents(C)| / |ents(A) ∪ ents(C)|
      if sim ≥ 0.82 AND jacc ≥ 0.30: attach, update centroid
  no match → new event, status='forming'
```

**Tune both thresholds against a hand-labelled set of 200 articles in Week 3.**
Measure purity and fragmentation. Do not guess and move on.

### 8.1 Activation — CHANGE F

v2.0 waited for a second article or 30 minutes before compiling. That is right
for ordinary news and wrong for the news that matters most: a single SEC filing,
a Fed emergency decision, or a CEO resignation from company IR is *the source of
truth* and there may never be a second source for twenty minutes. Waiting
guarantees you are late on your best stories.

```
if primary_source_strength >= 0.9:   activate immediately
elif article_count >= 2:             activate
elif age >= 30 min:                  activate
```

### 8.2 Event merge — CHANGE G (required by F)

The fast path creates duplicates: an 8-K activates at 09:41; three wire stories
about the same filing land at 10:05 and may form a second event.

```
on activation, and nightly:
  for each pair of active events, last 72h:
      if cosine(centroid_a, centroid_b) >= 0.88
         AND entity_overlap >= 0.50:
             merge → keep earliest first_seen
                     union articles and entities
                     set losing event merged_into = winner
                     recompile (one LLM call)
                     invalidate why_cache rows for both
                     re-render affected user_feed rows
```

Users who already received the first event get an update, not a second card.

**Known failure mode:** concurrent overlapping stories bleed together, and older
documents drag a cluster toward stale facts. The 72h window, the Jaccard gate,
and merge are the defences. Measure purity; don't assume it.

---

## 9. Compile, confidence, importance, relevance

### 9.0 `compile_event` — one structured LLM call

One call per event, strict JSON schema, temperature 0:

```json
{
  "event_type": "supply_disruption",
  "canonical_title": "...",
  "summary": "...",                        // neutral, ≤ 60 words
  "severity_class": "high",                // critical|high|moderate|low|routine
  "entities": [{"surface": "TSMC", "role": "subject"}],
  "claims": [{"text": "...", "article_id": "...", "directness": "stated"}],
  "event_time": "2026-09-12T04:10:00Z",
  "proposed_edges": [...]                  // QUEUED for review. Never auto-committed.
}
```

Regenerated only on material change (article count doubles, severity class
flips, or a merge). **Severity is a class, not a float** — five labels mapped to
fixed values: `critical 1.0 · high 0.8 · moderate 0.55 · low 0.3 · routine 0.15`.
Classes are far better calibrated than free-form numbers.

Note what is **not** in that schema any more: `extraction_uncertainty`. See 9.1.

### 9.1 Confidence — CHANGE E, fully observable

v2.0 said the LLM shouldn't judge its own trustworthiness, then gave its
self-reported uncertainty 10% of the confidence score. Internally inconsistent.
Removed. Confidence is now entirely observable:

```
C = 0.35 · source_quality_max     -- best source tier among evidence
  + 0.25 · independence           -- min(1, publisher_groups / 3)
  + 0.25 · directness             -- 1.0 stated, 0.6 inferred from co-mention
  + 0.15 · resolution_quality     -- 1.0 identifier, 0.7 exact alias, 0.4 fuzzy
```

`publisher_groups`, not domains — 40 sites carrying the same Reuters wire are
**one** independent source. Getting this wrong inflates confidence on syndicated
rumors, which is precisely the failure the push gate exists to prevent.

Sanity checks against the 0.6 push gate:

| Evidence | C | Result |
|---|---|---|
| One SEC 8-K, identifier-resolved, stated | 0.83 | pushes alone — correct |
| Three low-quality blogs, alias-resolved, inferred | 0.59 | just below the gate — correct |
| Three major wires, stated | 0.90 | pushes — correct |

**Path confidence = min(edge confidence) along the winning path.**

Model-derived uncertainty can return later **only** if it is demonstrated
calibrated against human labels. Until then: LLM as compiler, not judge.

### 9.2 Path score — CHANGE C

v2.0 used `DECAY^hops` at 0.55. Run the best possible two-hop story through it:
`0.55² = 0.3025`, so `R = 100(0.5×0.3025 + 0.3 + 0.2) = 65.1` — **below the push
threshold of 70**. A perfect two-hop event could not notify anyone. That is the
exact chain the product exists to deliver.

Explicit hop factors instead of exponential decay:

```
PathScore = seed_weight × Π edge_weight × hop_factor

hop_factor:   0 hops → 1.00
              1 hop  → 0.90
              2 hops → 0.75
              3 hops → 0.55
MAX_HOP 3 · FLOOR 0.05
```

| Scenario | PathScore | R |
|---|---|---|
| 2 hops, two 0.9 edges, importance 0.8, fresh | 0.61 | **74.4** — pushes |
| 2 hops, perfect, importance 1.0, fresh | 0.75 | 87.5 |
| 3 hops, perfect, importance 1.0, fresh | 0.55 | 77.5 — can push |
| 3 hops, 0.85 edges, importance 0.8, 0.9 fresh | 0.34 | 58.9 — feed only |

Store the winning path in `user_feed.path_json` — it is both the UI explanation
and the debug trace.

### 9.3 Event importance — CHANGE D

v2.0's `corroboration = max(primary_source_strength, independent_confirmation)`
let three worthless blogs from three publisher groups score `independent
confirmation = 1.0`, equal to an SEC 8-K. Quality has to gate independence:

```
importance = severity × corroboration

corroboration = max(primary_source_strength, independent_evidence)

primary_source_strength = max over evidence of source.tier_value
    1.00  SEC filing, regulator, exchange, central bank, court ruling
    0.90  company IR / official statement
    0.70  major wire (Reuters, Bloomberg, AP)
    0.50  quality secondary
    0.25  aggregator / blog

independence        = min(1, publisher_groups / 3)
independent_evidence = independence × avg_source_quality
```

| Evidence | corroboration |
|---|---|
| One SEC filing | 1.00 |
| Three major wires | 0.70 |
| Three low-quality blogs | 0.25 |

Volume no longer manufactures importance.

### 9.4 Relevance — ship three terms, log seven

```
R = 100 × (0.5 · PathScore + 0.3 · importance + 0.2 · freshness)
freshness = exp(-hours_since_publish / 18)
```

Also computed and stored with **weight 0**: portfolio exposure `P`, source
reliability `S`, novelty `N`, explicit interest `I`. At ≥ 2,000 feedback events,
fit weights by logistic regression on click-minus-dismiss. Guessing seven
weights with 25 users is false precision.

Top-level score is **additive**. Six sub-1.0 factors multiplied means any weak
term zeroes the score and nothing is interpretable. Multiplicative belongs inside
the path, not above it.

### 9.5 Confidence gate

Path confidence < 0.6 → **feed only, never push**. Now that confidence is
computed from evidence (§9.1), this gate actually does something.

---

## 10. Notification policy — percentile thresholds

The v2.0 bug in §9.2 was not really the decay constant. It was that a push
threshold of **70** was hard-coded without ever looking at the distribution it
sits in. Swapping constants and keeping a guessed threshold reproduces the same
failure somewhere new.

So thresholds are **percentiles of the observed score distribution**, recomputed
nightly over the trailing 7 days and written to `score_distribution`:

| Channel | Threshold |
|---|---|
| Push | score ≥ P97 (top 3%) |
| Feed | score ≥ P75 |
| Logged only | below P75 |

**Safety floors, absolute:**
- never push if score < 55
- never push if path confidence < 0.6
- during the first 14 days, before the distribution is stable, seed with
  P97 ≈ 70 and P75 ≈ 40

Hard delivery rules, non-negotiable:
- **Max 2 pushes/user/day.** Highest score wins.
- **Quiet hours** in user local time, default 22:00–07:00.
- **07:00 local daily digest is the default channel** — top 5 regardless of push
  eligibility. This is the retention mechanism; push is the accelerant.
- 30-minute cooldown between pushes.

---

## 11. API contract — freeze on Day 3

The frozen contract is the parallelism. Person B builds against a mock server
from Day 3 and never blocks.

| Method | Endpoint | Screen |
|---|---|---|
| `POST` | `/v1/auth/apple` | 1. Cold open |
| `GET` | `/v1/entities/search?q=` | 2. Onboarding (catalog autocomplete) |
| `POST` | `/v1/seeds` | 2. Onboarding |
| `GET` | `/v1/seeds/status` | 3. Graph construction |
| `GET` | `/v1/feed?cursor=&limit=` | 4. Feed home |
| `GET` | `/v1/events/{id}` | 5. Event detail |
| `GET` | `/v1/graph` | 7. Your graph |
| `GET` | `/v1/entities/{id}` | 8. Entity detail |
| `DELETE` | `/v1/seeds/{entity_id}` | 8. Remove from my world |
| `GET` `PATCH` | `/v1/profile` | 9. Settings |
| `POST` | `/v1/devices` | APNs registration |
| `POST` | `/v1/feedback` | every screen — instrument all of it |

```json
{
  "event_id": "evt_8f3a",
  "category": "SUPPLY CHAIN",
  "title": "Fremont plant halts production",
  "why": "Affects your TSLA — Tesla operates the Fremont plant, which has halted production.",
  "score": 81,
  "confidence": 0.94,
  "hops": 2,
  "importance": 0.8,
  "published_at": "2026-09-12T08:45:00Z",
  "article_count": 4,
  "primary_source": "company_ir",
  "entities": [{"id": "ent_tsla", "name": "Tesla Inc"}],
  "path": ["Fremont Factory", "Tesla Inc", "TSLA"],
  "slot_type": "graph",
  "slot_position": 1
}
```

---

## 12. Scope

### Ships in the beta — cutting any of these changes the product
- Shared temporal World Graph; bi-temporal edges; provenance on every edge
- Curated dependency edge set (~200 by Week 2, ~300 by beta end) — §6.2
- Demand-driven expansion, bounded at 3 hops, catalog/live split
- Multi-candidate alias resolution — §6.4
- Hybrid discovery: 15–20 push feeds + capped daily entity sweep
- Metadata-first three-tier ingest, quarantine, retroactive backfill, recall audit
- Event objects as the core unit, with fast-path activation and merge
- Deterministic confidence and importance — §9.1, §9.3
- Explicit hop factors and percentile thresholds — §9.2, §10
- One structured LLM call per event; zero per user; `why` cached per path shape
- Feed, event detail, your-graph, entity detail, settings on iOS
- Daily digest + capped push
- Full instrumentation, probe slots, shadow ranking — §16
- 7-day cold-start backfill on signup
- Baseline security — §20.1

### Deferred — sharpening, not structural
| Deferred | Why safe | Ship |
|---|---|---|
| Voice onboarding | Interface layer, same JSON as tap/type. 2 days vs 2 weeks. | Wk 9, before the VC demo |
| Broker connect (SnapTrade/Plaid) | OAuth + compliance + prod approval eats weeks. Manual tickers give the same signal. **Keep the normalized portfolio schema now.** | Wk 10–12 |
| Import from Claude/Codex memory | Cold-start trick, zero validation value | Post-beta |
| Provider abstraction + fallbacks | Write the interface, implement one | Post-beta |
| ER rungs 4–6 (fuzzy/embedding/LLM) | First 3 cover ~90% at 300 entities | Wk 10 |
| Auto-committing LLM-proposed edges | Queue and review; auto-commit needs a measured precision rate first | Wk 11 |
| Model-derived confidence signal | Only after calibration against human labels | Post-beta |
| Bulk Wikidata / GLEIF / Exhibit 21 / Form 4 / 13F | Curated edges beat bulk taxonomy at this size | When live entities > ~2,000 |
| Bayesian-surprise novelty | Logged at weight 0, turn on with data | Wk 12 |
| Kafka, ClickHouse, autoscaling, OTel | Infra for traffic we don't have | When metrics demand it |
| Android | — | After PMF |

**Test that a cut is safe:** if adding it later needs a data migration or schema
change, it is not safe. Bi-temporality, provenance, alias PK, and instrumentation
all fail that test, which is why they ship now.

---

## 13. Team split

### Person A — Backend / Graph / Pipeline (Aaryan)
Catalog + resolution, **curated edge set**, Neo4j model and all Cypher, ingest
and tiering, clustering and merge, `compile_event` prompt and schema, confidence
and importance math, impact engine, scoring, threshold recalibration, cost
monitoring.

### Person B — iOS / API surface / Measurement
SwiftUI app (Stitch designs exist — use them), auth, APNs, FastAPI read
endpoints, mock server, analytics events, probe-slot injection and
randomization, metrics dashboard, TestFlight and beta ops.

### Shared — changed only together, in the Friday review
API contract (§11) · scoring weights and thresholds (§9, §10) · anything moving
off the deferred list.

**Interface discipline:** Person B never waits on Person A. iOS develops against
fixture JSON from Day 3.

---

## 14. Eight-week plan

~20 focused hours/week each. Every week ends with something demoable.

### Week 1 · Sep 14–20 — Foundations
**A:** Postgres schema including the composite alias PK. Catalog from SEC ticker
file. Neo4j up with the edge model and the Cypher write module. Resolution rungs
1–3 with candidate disambiguation.
**B:** Xcode project, Apple Sign-In, **API contract frozen Day 3**, mock server,
Stitch screens → SwiftUI shells.
**Done:** five spellings of "SanDisk" return one `entity_id`; "Apple" returns two
candidates and disambiguates by context. iOS renders fixture events.

### Week 2 · Sep 21–27 — Graph substance + ingest
**A:** **200 curated edges over the top 30 entities** (§6.2), LLM-drafted and
human-accepted. Pre-warm ~50 entities. 15–20 sources with `publisher_group` and
`tier_value`. Metadata-first pipeline: url dedupe, metadata tagging, tiering,
conditional fetch, quarantine, embeddings, recall audit sampler.
**B:** Onboarding (tap/type), entity search, seeds, your-graph screen.
**Done:** you can trace `Apple → TSMC → ASML → Taiwan`, every edge provenanced.
Articles flowing and correctly tiered, with bodies fetched only where warranted.

### Week 3 · Sep 28–Oct 4 — Events *(highest slip risk — protected)*
**A:** Clustering. Hand-label 200 articles, tune both thresholds, measure purity
and fragmentation. `compile_event` with strict schema. Fast-path activation and
merge logic.
**B:** Feed home on real `/v1/feed`, event detail, reader, pull-to-refresh,
empty state.
**Done:** ~120 events/day. Cluster purity ≥ 0.85 on the labelled set. An 8-K
activates in under a minute and doesn't duplicate when the wires follow.

### Week 4 · Oct 5–11 — Impact and scoring
**A:** Confidence and importance, path scoring with hop factors, fanout,
`user_feed` writes, `why` templates + `why_cache`, shadow ranking stored.
25 more curated edges.
**B:** APNs end to end, settings, quiet hours, feedback wired to every tap,
probe-slot injection with randomized positions.
**Done:** a real event produces correctly ranked feed rows for the right users,
with a visible path and a shadow ranking stored alongside.

### Week 5 · Oct 12–18 — Delivery and measurement
**A:** Notification caps, percentile recalibration job, 07:00 digest, cold-start
backfill, `backfill_from_quarantine`, nightly decay and merge sweep, demand
report, cost dashboard. 25 more curated edges.
**B:** Metrics dashboard including utility scoring, TestFlight submitted.
**Done:** both of you have lived on the app for three days and can see your own
metrics.

### Week 6 · Oct 19–25 — Harden and dogfood
Both: fix what dogfooding exposed. Ranking sanity passes. Retries, dead-letter
queue. Template vs LLM `why` comparison. Recall audit reviewed. Recruit 25–30
from the tevarta.com waitlist. 25 more curated edges.
**Done:** seven days unattended. Feed p95 < 200 ms. Security checklist (§20.1)
signed off.

### Week 7 · Oct 26–Nov 1 — Beta in
25–30 users. Probe rate at **1-in-6**. Both on support. Daily metrics. Bug fixes
and obvious ranking corrections only — no feature work.

### Week 8 · Nov 2–8 — Read the data
Probe rate drops to 1-in-10 if volume allows. Ten user interviews. Fit relevance
weights on real feedback. Evaluate §17. Write the decision memo.

**Confidence: ~70%** by 8 Nov with the deferred list held. ~25% without it. Slip
concentrates in Week 3. **If Week 3 runs over, cut the Week 6 `why` A/B, not the
beta.**

---

## 15. Working cadence

- **Daily 15 min:** shipped / blocked. Not status theatre.
- **Friday 45 min:** demo on device, review metrics, agree next week's scope.
  Nothing gets added mid-week.
- **One board.** GitHub Projects. Every card has an owner and a "done means".
- **Trunk-based, small PRs, same-day review.**
- **`decisions.md`** — one line per decision with date and reason. In six weeks
  you will not remember why `hop_factor[2] = 0.75`.
- **Fixture-first.** Every endpoint gets a fixture before an implementation.
- **Curated edges go through PR review** like code. Two eyes on every edge.
- **Daily cost alert** to Telegram: LLM spend, search queries against cap, infra.
  A runaway loop should wake you, not appear on the invoice.

---

## 16. Measurement — CHANGE H

**Every user gets the graph feed.** No user gets a deliberately worse product.

### The kill criterion is observed, not counterfactual

v2.0 proposed comparing graph top-5 against shadow-keyword top-5. The flaw: the
user never saw the shadow ranking. If an event sat at graph position 1 and
keyword position 8, its click tells you nothing about how it would have
performed at position 8. Click rates are dominated by position.

So the primary comparison is between **things users actually saw**:

```
GRAPH SLOTS   vs   KEYWORD PROBE SLOTS
```

**Probe slots:** inject a keyword-only candidate — no traversal, no path scoring,
recency-ordered over seeded entities — tagged `slot_type='keyword_probe'`.

- **Rate: 1-in-6 for Weeks 7–8**, dropping to 1-in-10 afterwards. At 25 users ×
  ~10 cards/day × 14 days ≈ 3,500 impressions, 1-in-10 yields only ~350 probe
  impressions and maybe 35 clicks — too thin for a utility mean. 1-in-6 roughly
  doubles it.
- **Position: uniform random over slots 2–8.** Never slot 1, or you are measuring
  position rather than ranking.

**Utility score per impression:**

```
click        +1
save         +2
dismiss      -1
not relevant -2
no action     0
```

Compare `mean utility of graph slots` against `mean utility of probe slots`, and
report the per-user direction as a sign test.

### Shadow ranking — keep it, demote it
Continue computing and storing `shadow_score` / `shadow_rank` for every feed row.
It is valuable for offline analysis and for fitting weights in Week 8. It is
**not** a kill criterion, because it was never seen.

### Events logged, client and server
`app_open · feed_view · card_impression · card_click · card_dismiss ·
not_relevant · save · push_received · push_opened · digest_opened ·
seed_added · seed_removed · session_length` — each carrying `slot_type` and
`slot_position`.

House rule from the EDGAR repo: **no baseline control, no entry.** Probe slots
are the control, and unlike the A/B split, nobody gets a worse product for it.

---

## 17. Success and kill criteria — Day 14 of beta

| Metric | Ship it | Kill / rethink |
|---|---|---|
| Mean utility, graph slots ÷ probe slots | **≥ 1.5×** | ≤ 1.15× |
| Users with positive graph-vs-probe direction | ≥ 65% | < 50% |
| Median not-relevant rate | < 10% | > 25% |
| D7 retention (opened day 7) | > 40% | < 20% |
| Push → open rate | > 25% | < 10% |
| Cluster purity on weekly spot-check | ≥ 0.85 | < 0.70 |
| Tier-3 audit: articles containing live entities | < 10% | > 20% (filter too tight) |

Cost criteria — the v1 target of <$0.50/user contradicted its own fixed base:

| Cost metric | Target |
|---|---|
| **Marginal cost per additional user** | < $0.05/month |
| **Global processing cost per event** | < $0.02 |
| Total monthly burn through beta | < $250 |

Cost-per-user is meaningless at 25 users because the base is fixed. Marginal
cost per user is the number that predicts survival at 100,000 users — and by
construction it should be near zero.

If the graph doesn't beat keyword probes, the graph isn't earning its
complexity. Better to learn that for $250 than $250k. Honest prior with the
curated edge set in place: **~60/40 in favour.**

---

## 18. Cost model (25–50 users)

| Item | Monthly |
|---|---|
| `compile_event` (~120/day + merges/recompiles) | $14–28 |
| Curated-edge drafting (LLM proposals, one-off then trickle) | $5–10 |
| `render_why` (novel path shapes only) | $2–5 |
| Embeddings (~1,200/day incl. quarantine) | $4 |
| Search sweep (~50/day, capped) | $15–25 |
| **Compute — FastAPI + Celery workers** | **$15–30** |
| Postgres + Redis (managed, small) | $40 |
| Neo4j Aura (free → small) | $0–65 |
| R2 | $5 |
| Apple Developer ($99/yr, paid upfront — amortized) | $8 |
| Sentry (free tier) | $0 |
| **Total** | **$108–220/month** |

Ingest cost is flat in user count. The only line growing with users is
`user_feed` rows, which are bytes. That is the property the whole architecture
exists to preserve.

---

## 19. Risk register

| Risk | Impact | P | Mitigation |
|---|---|---|---|
| Event clustering underperforms | High | 0.5 | Labelled set Wk 3, extra week budgeted, fallback to entity+time-window grouping |
| Curated edge set too thin or wrong | **Existential** | 0.25 | 200 by Wk 2 then 25/week, PR review, `review_by` dates, weekly audit against surfaced events |
| Graph shows no lift over probes | Existential | 0.4 | Probe slots from Wk 4, 1-in-6 during beta; decide Day 14 |
| Metadata-first tagging loses recall | High | 0.35 | High-tier sources always fetched; 5% tier-3 audit; widen tagger if >10% |
| Duplicate events from fast path | Medium | 0.4 | Merge on activation + nightly sweep (§8.2) |
| Resolution ambiguity ("Apple", "Mercury") | Medium | 0.4 | Composite alias PK, context disambiguation, log every miss |
| Push fatigue → uninstalls | High | 0.3 | 2/day cap, digest default, quiet hours, percentile thresholds |
| Search sweep cost overruns | Medium | 0.25 | Hard monthly cap in code, alarm at 80% |
| Source blocks / ToS | Medium | 0.3 | Feeds only, robots.txt, link out, no body text shown |
| App Store rejection | Medium | 0.2 | TestFlight only; no financial-advice framing |
| Neo4j ops overhead for 2 people | Medium | 0.25 | Managed Aura; at 300 nodes, Postgres recursive CTEs are a viable fallback |
| Scope creep off the deferred list | High | 0.6 | §12 is the contract; changes need both people in the Friday review |

---

## 20. Legal and ops

- **Never display source body text.** Title + our summary + link out.
- **No financial advice framing.** "This may affect your position" is
  information; "you should sell" is regulated. Get a lawyer's read before public
  launch.
- **Data deletion.** A user deletes their account and all seeds; the World Graph
  is unaffected because it holds no user data. A quiet benefit of the shared
  design.
- **TestFlight for the whole beta.** No public App Store release before Week 9.

### 20.1 Security — baseline for beta

Not a security project. But you are storing what people invest in and care
about, which is sensitive even though it isn't regulated. This list is the
minimum, and it is signed off in Week 6 before any external user is let in.

**Identity and access**
- Validate Apple identity tokens **server-side** — never trust a client-supplied
  `apple_sub`
- Short-lived access tokens (≤ 1 hour) with refresh; revoke on sign-out
- `user_id` authorization check on **every** user-owned endpoint. Write one
  dependency and apply it everywhere — most auth bugs are a missing check on one
  route, not a broken scheme.
- Rate-limit `/v1/auth/*` and `/v1/feedback` specifically

**Secrets and credentials**
- Secrets only in a managed secret store. Never in the repo, never in
  `decisions.md`, never in a Telegram alert.
- **Separate dev and production credentials and databases.** No shared DB.
- Rotate anything that ever appeared in a log or a screenshot

**Data**
- TLS everywhere, HSTS on the API
- Managed encryption at rest on Postgres, Neo4j, and R2
- **Never log APNs tokens, access tokens, or `apple_sub`.** Add a log redaction
  filter in Week 1 rather than auditing for leaks in Week 6.
- Store APNs tokens encrypted (`devices.apns_token_enc`)
- Automated daily backups on Postgres and Neo4j, with **one restore actually
  tested** before beta. An untested backup is not a backup.

**Operations**
- Sentry configured to scrub request bodies and headers
- Dependency scanning on the repo (GitHub Dependabot is enough)
- Account deletion endpoint that genuinely removes `user_seeds`, `user_feed`,
  `feedback`, `devices`, and `users` — tested, not assumed

---

## 21. Open questions — resolve in Week 1

1. Which 15–20 sources? Bias to primary. Write the list on Day 1 with
   `publisher_group` and `tier_value` assigned — §9.1 and §9.3 both depend on
   those being right.
2. Which embedding model? Cost and dimensionality fix the pgvector schema —
   decide before Week 2.
3. Initial `weight` per relation type (14 values). Hand-set them, record the
   reasoning in `decisions.md`, revisit with data.
4. Which 30 entities get curated edges first? Derive from the waitlist if you
   can survey it; otherwise most-held US names plus your own positions.
5. Who is Person B, and are they committed to eight weeks at 20 hrs/week?
   Everything above assumes yes. If not, this is a 14-week plan.

---

## Founder notes — the sequencing that actually matters

1. **Week 2 decides the beta.** Not the ingest — the curated edge set.
   Everything downstream measures the quality of those edges. A perfect pipeline
   over a taxonomy graph produces a confident, useless answer in Week 8.

2. **Freeze the API contract on Day 3.** With two people, the contract *is* the
   parallelism. Every day it stays fluid is a day someone is blocked.

3. **Ship measurement with the feature.** With 25 users you get one clean read.
   Instrumented from Week 4 or the beta teaches you nothing.

4. **Protect the deferred list like a budget.** Every item on it is genuinely
   attractive, and each one is how eight weeks becomes sixteen. Voice onboarding
   will feel urgent because it demos well — Week 9 for the pitch, not Week 2 for
   the beta.

5. **Dogfood from Week 5.** You will find the ranking problems faster than any
   beta user will report them.

6. **Numbers, not structure, are where this breaks.** Every serious flaw across
   three review rounds was a number that didn't mean what it claimed —
   confidence from an uncalibrated source, importance from article volume, a
   decay constant that made the core use case unpushable, a threshold with no
   distribution behind it, a lift test with no statistical power, a cost target
   contradicting its own model. The architecture survived every round untouched.
   Aim future reviews at the numbers.

7. **The moat is time-in-market on the graph, not the architecture.** This design
   is copyable in six months. What isn't copyable is two years of accumulated
   temporal edges, invalidations, curated dependencies, and relevance feedback
   that nobody else was recording. So start recording — correctly, with
   provenance and validity intervals — as early as possible. That is why Week 1
   is the schema and not the demo.

---

**Document frozen 12 Sep 2026. Next revision: Week 5, from dogfooding evidence.**
