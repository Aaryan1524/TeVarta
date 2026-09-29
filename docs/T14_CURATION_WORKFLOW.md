# T14 — Curated edge validation, drafting, review, and loading

**Owner:** Aaryan; Aaditya accepts evidence on every PR that adds edges
**Branch:** `aaryan/phase-2/t14-curation-loader`
**MVP:** §5.2 edge properties, §6.2 curated dependency edges, §9.1 confidence

Curated edges are hand-accepted, but they carry the same provenance, computed
confidence, and bi-temporal validity as extracted edges. An LLM may draft candidates.
Only a named human decision puts an edge in the file, and only reviewed content can
reach Neo4j.

## Files

| Path | Role |
|---|---|
| `graph/curated_edges.yaml` | Loadable set: hand-declared non-SEC `entities`, review-written `edges` |
| `graph/curation/review_log.jsonl` | Append-only accept/edit/reject records, including rejected drafts |
| `graph/curation/proposals/<batch>.yaml` | Draft batches awaiting decisions; commit them with the review |
| `backend/app/curation.py` | Schema, SHA provenance, review, strict resolution, load plan |
| `backend/app/curation_draft.py` | Metered, fail-closed LLM drafting |
| `scripts/curate_edges.py` | `draft`, `review`, `validate`, `load`, `show` |

## Edge schema

```yaml
- source: TSM                 # ticker, CIK, entity_id, or a declared alias
  relation: DEPENDS_ON        # one of the frozen 14
  target: ASML
  weight: 0.9                 # (0, 1]; relation importance, set by the reviewer
  directness: 1.0             # 1.0 stated, 0.6 inferred (§9.1)
  rationale: "Sole commercial supplier of EUV lithography systems"
  evidence:                   # one or more; drives computed confidence
    - url: https://www.sec.gov/...           # real https URL; placeholders rejected
      source_type: sec_filing                # §9.3 tier vocabulary
      publisher_group: tsmc                  # originating publisher, not domain
      locator: "Form 20-F 2024, Item 3.D"    # optional
  valid_from: 2024-12-31      # when it became true in the world (≤ curated_at)
  curated_by: aaryan          # the accepting human (written by `review`)
  curated_at: 2026-09-28      # observed_at; written by `review`
  review_by: 2027-03-28       # re-review date, after curated_at
  proposal_id: tsm-20260928-01
  drafted_by: llm:<model> | human
```

`confidence` is rejected if present. The loader computes it as §9.1:
`0.35·max tier + 0.25·min(1, publisher_groups/3) + 0.25·directness + 0.15·resolution`.
Resolution quality is 1.0 for an identifier (ticker, CIK, entity_id) and 0.7 for an
exact alias. `source_type` tiers are: `sec_filing`, `regulator`, `exchange`,
`central_bank`, `court` 1.0; `company_ir` 0.9; `major_wire` 0.7;
`quality_secondary` 0.5; `aggregator` 0.25.

Non-SEC endpoints such as countries, commodities, and private suppliers are declared
under `entities` with an `ent_cur_` ID, a frozen entity type, and aliases.

## Provenance and the review gate

Each edge's `curated:<sha>` is the SHA-256 of its canonical JSON. That includes the
evidence, dates, reviewer, and proposal ID, but not YAML formatting or key order. The
loader writes an edge only when that exact SHA appears in an `accepted` or `edited`
review record, so:

- an edge added by hand, or edited after review, fails `validate` and `load`;
- `show curated:<sha>` returns the reviewed text and its review record, including
  superseded revisions that now exist only in the log.

A test also enforces the gate on the committed files, so CI fails on an unreviewed
edge.

## Workflow

```sh
# 1. Draft (optional). Needs OPENAI_API_KEY, TEVARTA_CURATION_DRAFT_MODEL,
#    TEVARTA_CURATION_DRAFT_USD_PER_MTOK_INPUT/_OUTPUT, and a reachable Postgres.
python scripts/curate_edges.py draft --batch-id tsm-20260928 --subject TSM \
  --text-file item1a.txt --evidence-url https://www.sec.gov/... \
  --source-type sec_filing --publisher-group tsmc --locator "Item 3.D" \
  --out graph/curation/proposals/tsm-20260928.yaml --database-url "$TEVARTA_POSTGRES_URL"

# 2. Decide. For each proposal, set `decision: accept` after completing `edge`
#    (valid_from, review_by, weight, and evidence), or `decision: reject` with a
#    `reason`. Leave `draft` untouched: it is SHA-checked.

# 3. Record decisions. This appends to the log and re-renders curated_edges.yaml.
python scripts/curate_edges.py review graph/curation/proposals/tsm-20260928.yaml --reviewer aaryan

# 4. Check resolution and computed confidence against the catalog (read-only).
python scripts/curate_edges.py validate --database-url "$TEVARTA_POSTGRES_URL"

# 5. After the PR is merged with Aaditya's evidence acceptance, load.
TEVARTA_NEO4J_PASSWORD=... python scripts/curate_edges.py load \
  --database-url "$TEVARTA_POSTGRES_URL" --neo4j-url "$TEVARTA_NEO4J_URL"
```

Hand-written batches use `drafted_by: human`, `usage_request_id: null`, and
`draft: null`.

Drafting details:

- The model extracts only surface forms, a relation, a verbatim excerpt, a
  rationale, and whether the text states or implies the link.
- `excerpt_verified: false` flags an excerpt that is not in the source text.
- Weights are prefilled from the proposed T01 relation defaults, for the reviewer to
  confirm.
- An unchanged accepted draft is recorded as `accepted`. A draft whose source,
  relation, target, rationale, or directness changed is recorded as `edited`.
- The model call fails closed without configuration, and it never runs if the usage
  database is unreachable.
- Every call writes an `llm_curation_draft` receipt to `usage_records` with tokens,
  cost, and latency, and failed calls are included.

## Loading and idempotency

`load` validates the whole file before it writes anything. Specifically, it:

1. upserts declared entities and their aliases, marked `source='curated'`;
2. resolves every endpoint strictly: an identifier or exactly one alias, never a
   `seed_count` guess, and no `resolution_misses` noise;
3. plans all revisions, rolling back Postgres if anything fails;
4. writes each edge through `GraphStore.create_curated_edge_revision`. That method
   accepts only `origin='curated'` with `curated:<sha256>` provenance, while the
   general write path still rejects curated edges.

The graph request ID is `uuid5(curated:<sha>)`. Every revision field is a pure
function of the reviewed entry and its resolved endpoints, so reloading returns
`replayed` and never duplicates an edge.

To revise an edge, review a new proposal for the same source, relation, and target
with a later `valid_from`. `review` replaces the file entry, the old text stays
resolvable through the log, and on load the new graph revision closes the old one.
A revision whose `valid_from` is not later is refused at review. At load, it is
refused as a stale revision, with guidance. If an endpoint later resolves to a different entity, the
edge fails loudly instead of being silently rewired.

## Verification

- `pytest backend/tests/test_curation.py backend/tests/test_curation_draft.py
  backend/tests/test_graph_store.py`: schema, gate, SHA, review, drafting, metering,
  and the write boundary.
- `make database-test`: strict Postgres resolution, a load plan against the real
  catalog, and costed usage receipts.
- `TEVARTA_TEST_NEO4J_URI=... pytest backend/tests/test_curation_integration.py`:
  reload replays with one history row, a later revision supersedes, and a same-date
  revision is refused. CI does not run Neo4j tests.

## Out of scope / carryover

- Real edges and the coverage report (T15).
- Retiring a curated edge and acting on `review_by` expiry. Planned for
  `decay_and_invalidate` (T48); the graph invalidation primitive already exists.
- Queueing `compile_event` `proposed_edges` into this review flow (T36).
