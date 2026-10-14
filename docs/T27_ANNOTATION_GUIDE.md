# T27 — Clustering benchmark: annotation guide

**Owner:** Aaditya coordinates; both founders label and resolve disputes
**MVP:** §8 clustering ("hand-label 200 articles … measure purity and fragmentation"),
§8.2 merge, §14 Week 3, §19 clustering risk

The benchmark is the only evidence for the two §8 thresholds (`sim ≥ 0.82`,
`jacc ≥ 0.30`) and the §8.2 merge gates (`0.88`, `0.50`). Labels are human. A
grouping the collector suggests is a hint and is never copied into a label file
unreviewed.

## What is one event

An event is **one real-world occurrence** that a reader would want as a single card.

Same event:

- A filing and the news stories that report on that filing.
- Syndicated copies of one wire story (different sites, same text).
- Updates of the same story ("UPDATE 1-", "UPDATE 2-"), even with new details, when
  published within 72 hours.
- Different publishers covering the same occurrence from different angles.

Different events:

- Two filings by the same company (an 8-K on earnings and an 8-K on a CEO change).
- The same company in two unrelated stories on the same day (a recall and a lawsuit).
- An occurrence and its consequence reported days later as its own news (a quake,
  then a week later "TSMC says output fully restored"). When in doubt, ask: would a
  reader who saw the first card treat this as new news?
- A background or explainer piece that mentions an earlier occurrence (a *stale
  document*) is its own event, or its own singleton, unless it reports something new
  about that occurrence.

Every article gets an event key. An article alone in its event still gets a unique
key (`evt-<short-slug>`); never leave a label blank.

## Case quotas

The 200 articles must exercise every §8 failure mode. `case` on each item records
why it was picked:

| case | at least | what it tests |
|---|---:|---|
| `filing_then_wires` | 20 | §8.1 fast path: a primary filing, then follow-up wires (§8.2 merge) |
| `syndication` | 20 | one story across many publishers (dedupe, not new events) |
| `concurrent` | 20 | several distinct stories about the same entity in the same 72 h (bleed) |
| `ambiguous_entity` | 10 | "Apple", "Mercury", common-word names (§6.4) |
| `stale_document` | 10 | older documents that drag a cluster toward stale facts |
| `ordinary` | rest | typical tier-1 news |

## Process

1. **Collect.** `python scripts/benchmark.py collect --database-url … --days 14` writes
   `candidates.jsonl` with case hints and an *unreviewed* `suggested_event`
   (entity + 72 h, the §19 fallback). Curate 200 into `items.jsonl`, meeting the
   quotas.
2. **Label independently.** Each founder writes `labels/<name>.jsonl` (`{id, event}`)
   without looking at the other's file. Event keys don't need to match across
   annotators; only the grouping is compared.
3. **Compare.** `python scripts/benchmark.py agreement labels/aaryan.jsonl
   labels/aaditya.jsonl` prints pairwise same-event F1 and every disputed article.
4. **Resolve.** Discuss each disputed article together and write
   `labels/resolved.jsonl`. Add a `resolution` note for any non-obvious call; it
   becomes a rule in this guide.
5. **Validate.** `python scripts/benchmark.py validate` passes only when there are
   exactly 200 articles, every quota is met, and all three label files cover every
   article. Commit the dataset as `v1`; later changes are `v2`, never edits to `v1`.

## Metrics (`backend/app/benchmark.py`)

- **Purity**: the share of articles that belong to the majority gold event of their
  predicted cluster. §17 gate: ≥ 0.85 ship, < 0.70 rethink.
- **Fragmentation**: Σ(k_e − 1) / Σ(|e| − 1) over gold events with at least two
  articles, where k_e is the number of predicted clusters an event is split across.
  0 means no event is split; 1 means every multi-article event is fully split.
- **B-cubed F1**: a diagnostic that balances over-merging against over-splitting.

`python scripts/benchmark.py score predicted.jsonl` scores any clustering: the §8
algorithm at a threshold pair, the §19 entity+time fallback, or a merge sweep.
Purity rewards splitting everything, and fragmentation rewards merging everything,
so read them together.
