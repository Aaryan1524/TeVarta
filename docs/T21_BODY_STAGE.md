# T21 — Conditional fetch, body dedupe, body tagging, and embeddings

**Owner:** Aaryan
**MVP:** §3 components 6–9, §5.1 `articles`, §7.2 change B, §7.4 content rights, §18

`backend/app/bodies.py` gives each tier exactly its §7.2 treatment.
`app.discovery.process_candidate` (T20) queues `app.bodies.process_article` for every
new tier-1 or tier-2 article.

| Tier | Body fetched | Embedding |
|---|---|---|
| 1 | yes | body (title + first ~24k chars) |
| 2 | no | title + snippet |
| 3 | no | none |

T22 adds the high-tier override and the 5% audit sample through
`process(article_id, fetch=True)`. A forced fetch keeps the tier's embedding policy,
so a sampled tier-3 article is never embedded.

## Fetch

- Fetches only with a declared contact (`TEVARTA_HTTP_CONTACT`) and a configured
  private store (`TEVARTA_BODY_STORE_DIR`). Without either, the article is
  `refused`.
- **robots.txt** is read with our User-Agent and cached per host for a day. A 404
  allows; 401, 403 and 5xx disallow. A disallowed URL is never requested.
- At most 3 MB per response. Only HTML, plain text or XML is accepted. Scripts,
  styles, navigation, headers and footers are dropped before text extraction.
- Bodies are stored content-addressed (`bodies/<sha256>.txt`) and never served to
  clients (§7.4). `LocalBodyStore` is the development store; an R2 store implements
  the same `put` when credentials exist.

## Body dedupe

64-bit SimHash over word 3-shingles, stored as a signed `BIGINT`. A body within
**12 bits** of an already-stored body published within ±7 days is a near duplicate.
The new article keeps its own row and publisher, since that is provenance for
corroboration, but reuses the stored body key and the tier-1 embedding. Nothing is
fetched, stored or embedded twice.

The threshold was measured on SEC filing and newsroom text: a byline, an
"UPDATE 1-" prefix, word swaps or 5% truncation stay at 10 bits or fewer, while
unrelated passages are at least 21 apart (see `decisions.md`).

## Body tags

The same deterministic tagger runs over the first 20k characters. New entities are
inserted with `found_in='body'`, and ambiguities are logged to `resolution_misses`
with `"stage": "body"`. The metadata tier is never rewritten (pending decision).

## Embeddings and metering

`OpenAIEmbedder` requests `text-embedding-3-small` at `dimensions: 1024` and rejects
any other vector size. It fails closed without `OPENAI_API_KEY` **and**
`TEVARTA_EMBEDDING_USD_PER_MTOK`, because unmetered spend is not allowed.

Each call writes `usage_records` (`embeddings`: tokens, cost, latency, and failures
too). Each stored body writes `r2` with its byte count. A missing or failing embedder
never loses the stored body.

## Verification

- `test_bodies.py` 20 passed: SimHash range and stability, measured syndication
  tolerance, page-furniture removal, robots rules and status handling, daily robots
  cache, refusal of 404 / PDF / empty pages, a disallowed URL never requested,
  content-addressed store, a 1024-dimension request, a wrong-size vector rejected,
  fail-closed configuration.
- `test_bodies_database.py` 8 passed, covering:
  - a tier-1 article stored, body-tagged, embedded, and metered for bytes and tokens;
  - a syndicated copy reusing the body and embedding, with one embed call;
  - tier 2 embedding the title and snippet without a fetch;
  - tier 3 getting nothing;
  - a forced tier-3 fetch not being embedded;
  - refused and failed fetches;
  - a missing or failing embedder still keeping the body;
  - T20 queuing tier 1 and 2 only.
