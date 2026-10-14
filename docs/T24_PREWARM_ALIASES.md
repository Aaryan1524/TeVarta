# T24 — Pre-warm and reviewed aliases

**Owner:** Aaryan
**MVP:** §6.1 (live entities are "deeply resolved: aliases"), §6.3 pre-warm, §14 Week 2

## Reviewed short names: `graph/entity_aliases.yaml`

SEC titles make poor surface forms, so ordinary headlines never tagged these
companies:

| SEC title | Normalized alias | Headline says |
|---|---|---|
| AMAZON COM INC | `amazoncom` | Amazon |
| ASML HOLDING NV | `asmlholdingnv` | ASML |
| TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD | `taiwansemiconductormanufacturing` | TSMC |

Each entry names its entity strictly (ticker, CIK or `entity_id`; never a name) and
gives the names news uses, with a rationale. `apply_aliases` validates the whole file
against the catalog, then writes everything or nothing. It refuses an alias that
already names another entity unless the entry says `shared: true`. Aliases are
written with `source='reviewed_alias'` and `confidence 0.9`, and loading is
idempotent. The file is reviewed in PRs like code; avoid common words (`Cadence` is
both a word and another company, so the entry uses `Cadence Design`).

## SEC state tags

About 550 of 8,028 SEC titles (7%) end in a state-of-incorporation tag:
"APPLIED MATERIALS INC /DE", "QUALCOMM INC/DE", "ARM HOLDINGS PLC /UK". Normalizing
folded the tag into the alias (`appliedmaterialsde`). The catalog import now also
writes the alias without the tag (`appliedmaterials`). Four stripped names coincide
with another company's name; the composite alias key and §6.4 disambiguation already
handle that.

## Pre-warm: `config/prewarm.yaml`, `scripts/prewarm.py`

The list has 50 likely early seeds: the 30 T01 curation targets plus 20 widely held
US names. The command:

1. loads the reviewed aliases;
2. queues expansion for every listed entity (same generation rule as republish);
3. runs the T23 dispatcher until nothing is pending;
4. prints coverage: live count, entities with dependency edges, and gaps.

A gap is an entity with no dependency edges or no ready view. It is reported, never
filled with invented edges; T15 curation closes it. `--no-search` skips the 30-day
targeted search to save budget.

```sh
TEVARTA_NEO4J_PASSWORD=… python scripts/prewarm.py \
  --database-url "$TEVARTA_POSTGRES_URL" --neo4j-url "$TEVARTA_NEO4J_URL" [--no-search]
```

### Views converge

Pre-warm exposed an ordering effect. NVIDIA expanded before TSMC in one batch, so
NVIDIA's view missed TSMC's newly added industry edge, and a second run changed the
view. Now, when an expansion adds a structured edge, every ready view containing that
entity is re-queued. Three consecutive real runs produce identical views (same hash
over all views).

## Real run (development catalog, live EDGAR, test Neo4j holding the batch-1 dry run)

- 50/50 listed entities are live with ready views. A rerun changes nothing: aliases
  `added: 0`, views identical.
- 83 live entities: 53 companies, 27 SEC industries (live EDGAR), 3 countries.
- 8/50 have dependency edges: the batch-1 companies, with NVIDIA reaching 10 edges
  and Apple 8. **42 are gaps** until later curation batches land.

## Verification

- `test_aliases.py` 14 passed: committed alias file and pre-warm list valid; seven
  invalid-file cases; state-tag aliases.
- `test_aliases_database.py` 6 passed: import writes the stripped alias; strict
  resolution (ticker, CIK, ID; never a name); idempotent load; collision refused
  with nothing written; declared sharing allowed; an unknown entity fails the file.
- `test_phase2_integration.py` (+2): pre-warm promotes, publishes and is idempotent
  with identical coverage; an entity without dependencies is reported as a gap.
