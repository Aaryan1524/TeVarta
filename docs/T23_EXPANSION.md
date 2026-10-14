# T23 — First-seed expansion

**Owner:** Aaryan
**MVP:** §1.1 on-demand World Graph, §3 component 20, §6.1 catalog vs live, §6.3, §9.2
**Closes for T25:** the four handoff obligations recorded in #35

A first seed of a catalog entity makes one reusable, bounded live subgraph. A second
seed of the same entity reuses it: no new request, ready at once.

## Flow

```
POST /v1/seeds (T25) ─► entity_expansion_requests (pending)
beat: app.expansion.dispatch_expansions every 15 s
  claim ≤5 requests  (FOR UPDATE SKIP LOCKED: one worker per entity)
  expand(entity):
    upsert node · EDGAR SIC → IN_INDUSTRY (structured, 0.20)
    bounded upstream traversal (one named graph read per hop)
    promote seed + every reached entity to live
    publish entity_graph_views (ready, v1-shaped GraphResponse)
  finish (only by the claim that owns it): complete · pending again if a republish
          arrived mid-run · failed (retry ×3, 5-minute linear backoff;
          a running claim older than 15 min is reclaimed)
```

`generation` is bumped by every republish request, even while a request is running,
so a worker that read the old graph finishes `pending` instead of `complete`. A
worker whose claim was reclaimed cannot overwrite its successor: finishing matches
`claimed_at`. The dispatcher opens no Neo4j connection when nothing is pending.

## Traversal

`graph_store.IMPACT_DIRECTION` fixes which way each relation carries an event's
impact, and T37 propagation must use the same table. From a subject, the upstream
neighbours are the targets of outgoing `forward`/`both` edges and the sources of
incoming `reverse`/`both` edges:

| Direction | Relations |
|---|---|
| forward: the target affects the source | DEPENDS_ON, MANUFACTURES_AT, EXPOSED_TO, OPERATES_IN, REGULATED_BY, IN_INDUSTRY, PRODUCES, CUSTOMER_OF |
| reverse: the source affects the target | SUPPLIES, SOLE_SOURCE_FOR, OPERATED_BY |
| both | SUBSIDIARY_OF, LISTED_AS, COMPETES_WITH |

Bounds (§6.3, §9.2, scores from `scoring.path_score` with seed weight 1.0):

- Hop 1 is always walked.
- Hop 2 is walked only through a hop-1 edge with weight ≥ 0.7.
- Hop 3 is walked only from a hop-2 node with path score ≥ 0.15.
- Anything below 0.05 is dropped, and traversal stops at 3 hops.

Each node keeps its best path. Only current edges are read: `valid_until` and
`invalidated_at` must both be null.

**What upstream-only means (pending founder decision).** From Apple, the walk reaches
TSMC, Taiwan, ASML, Carl Zeiss and the Netherlands, but never NVIDIA, which is only
another TSMC customer. The flip side: a TSMC or ASML holder receives no customer-side
news, so an Apple demand shock does not reach TSMC holders. If demand linkage is
wanted later, the clean extension is a path that may go down but never switch back
up, which still keeps co-customers out.

## Prepared view

`{nodes: [{id, name, entity_type}], edges: [{source_id, target_id, relation,
confidence}]}`, sorted and deterministic. Node types use the v1 API vocabulary
(`company`, `place`, …). Types v1 lacks (`industry`, `macrofactor`, …) are written in
lowercase until the founders settle the T25 enum conflict.

## The four T25 obligations

1. **Views for every live entity.** Expansion publishes a view for any seeded
   entity, live or not. Pre-warm (T24) uses the same path.
2. **Retry failed views.** Failures retry three times. The user sees `building`
   until the last attempt fails; a later seed revives an exhausted request. A failed
   republish keeps the last ready view. If the industry edge cannot be written,
   enrichment reports `unavailable` and expansion still completes.
3. **Republish on curated changes.** `curate_edges.py load` re-queues every
   published view whenever a revision was written (not on pure replays).
4. **v1 types, newest wins.** Views use v1 types; T25 merges overlapping views by
   `updated_at`.

## EDGAR industry

`data.sec.gov/submissions/CIK…json` → `sic`, `sicDescription` → `ent_sic_<code>`
(Industry) and `IN_INDUSTRY` (weight 0.20, origin `structured`, source
`edgar:sic:<cik>`, confidence 0.83 = one SEC record, stated, identifier). This is
enrichment: without `TEVARTA_HTTP_CONTACT`, or if EDGAR fails, expansion continues
and the report says `skipped` or `unavailable`. The edge is written once. Its
`valid_from` is the catalog row's creation time, since the SIC holds at least that
long, and `observed_at` is now.

## Not yet in this slice

- The targeted 30-day search runs through the shared Brave budget when
  `TEVARTA_SEARCH_PROVIDER=brave` and `BRAVE_SEARCH_API_KEY` are set; otherwise the
  report says `skipped`. It runs after the view is published, so a slow or failed
  search never delays onboarding (`failed`, `budget_exhausted`).
- `backfill_from_quarantine` and the 7-day article backfill are T47 (Phase 5).
- Short company names ("TSMC", "ASML", "Amazon") need reviewed aliases. That is
  T24's alias file; until it lands, headlines match only the SEC legal name.

## Verification

- `test_expansion.py`: Apple's batch-shaped supply chain with exact scores; NVIDIA
  and industry peers not reached; the industry node never expanded; boundaries at
  0.7, 0.15 and 0.05; the 3-hop stop; best path; one read per hop; a view T25
  accepts.
- `test_expansion_database.py`: 8 concurrent dispatchers claim each request once;
  retry backoff and the attempt cap; stale reclaim; failed republish keeps the
  ready view; republish requeues; re-seed revives; 0005 migration roundtrip.
- `test_graph_store_integration.py`: the Cypher read follows impact direction and
  ignores invalidated edges.
- `test_phase2_integration.py` (PostgreSQL + Neo4j, Graph CI job), the Phase 2
  integration proof:
  - a Taiwan quake headline is tier 2 before the seed;
  - TSMC is seeded through the onboarding store;
  - four racing dispatchers produce one expansion;
  - TSMC, Taiwan and its industry become live, while NVIDIA and Apple do not;
  - the view is served;
  - the same headline, and the company by its legal name, are then tier 1;
  - a second user's seed is ready with no new work;
  - republishing never duplicates the industry edge.
