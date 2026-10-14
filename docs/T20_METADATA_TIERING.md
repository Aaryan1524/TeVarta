# T20 — URL dedupe, metadata tagging, and the tier gate

**Owner:** Aaryan
**MVP:** §3 components 3–5, §6.4 resolution, §7.2 article flow (change B), §7.3 tiers

`backend/app/ingest.py` turns one discovery candidate into one `articles` row with a
tier. It reads metadata only: the stage holds no HTTP client, so no body can be
requested before tiering. T21 owns bodies; T22 owns quarantine, the high-tier fetch
override, and the recall audit.

## Flow

```
candidate (url, title, snippet, publisher, published_at, source_hint)
  → resolve source            sources row; search hosts map to a configured domain or
                              enter as unvetted `web:<host>` (tier_value 0.25)
  → url_hash                  SHA-256 of the normalized URL; already seen → duplicate
  → tag title + snippet       one batched alias lookup
  → tier                      live entity → 1 · catalog only → 2 · none → 3
  → INSERT … ON CONFLICT (url_hash) DO NOTHING
  → article_entities (found_in='metadata'), resolution_misses for ambiguity
```

The Celery task `app.discovery.process_candidate` runs the stage. `poll_sources`
mirrors `config/sources.yaml` into `sources` before it polls, after its fail-closed
contact check.

## URL identity

`normalize_url` lowercases the host, drops `www.`, treats http and https alike,
removes default ports, fragments, duplicate and trailing slashes, and tracking
parameters (`utm_*`, `gclid`, `fbclid`, …), and sorts the remaining query. Path case
is kept. The insert is race-safe: concurrent workers on one URL produce one row.

## Tagging

- **Identifiers (rung 1):** `$AAPL`, `NASDAQ: AAPL`, `2330.TW`, `CIK 320193`, via
  `identifier_alias(bare=False)`. For `sec.gov` sources only, a parenthesized
  10-digit filer CIK in the title (`APPLE INC (0000320193)`) is an identifier.
  Identifier text is masked before name matching, so `NASDAQ: AAPL` does not also tag
  Nasdaq Inc.
- **Names (rungs 2–3):** n-grams of up to six words within punctuation-bounded
  segments, normalized with `normalize_alias`. A name must be a proper-noun span:
  every word capitalized or numeric, with lowercase connectors (`of`, `and`, `the`,
  …) allowed only inside it. "inflation target" is not Target Corp. The longest
  matched span wins over its parts.
- **Ambiguity (§6.4):** several entities for one alias are settled by co-occurrence
  with entities already tagged in the article, then `seed_count`; a remaining tie
  tags nothing and writes `resolution_misses` with the candidates and
  `"stage": "metadata"`.
- **Salience:** 1.0 for a title mention, 0.6 for snippet-only.

### Known limit (recorded in `decisions.md`, pending)

Capitalization is the only common-word guard. A sentence-initial or title-cased
common word that equals a company alias ("Visa Fees Rise", "Target Missed") is
tagged. For a catalog entity that costs a quarantine row; for a live entity it costs
one body fetch and embedding. The 5% tier-3 audit (T22) and `resolution_misses`
measure the effect; rungs 4–6 stay deferred to Week 10 per §6.4.

## Verification

- `pytest backend/tests/test_ingest.py`: URL identity, HTML cleanup, mention
  extraction, identifier masking, SEC filer CIK, longest match, proper-noun rule,
  co-occurrence and refusal, salience, one batched lookup, tier gate.
- `make database-test` (now every `database`-marked module): live Taiwan → 1,
  catalog → 2, none → 3; equivalent URLs once; 16 concurrent workers → one row;
  ambiguity logged; no network call during the stage; a high-tier source keeps its
  metadata tier; source sync and web-host mapping; SEC Atom entry through the real
  poller to tier 1; the Celery task commits and replays as a duplicate.
