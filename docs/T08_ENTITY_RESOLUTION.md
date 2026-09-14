# T08 — Entity resolution rungs 1–3

**Owner:** Aaryan
**Branch:** `aaryan/phase-1/t08-entity-resolution`

The resolver handles CIK, ticker, exchange-qualified ticker, and normalized exact
aliases only. Multi-candidate names are ranked first by provided article-context
co-occurrence and then by `seed_count`; a remaining tie returns candidates with no
tag and writes `resolution_misses`. Fuzzy, embedding, and LLM resolution remain
out of scope until the frozen Week 10 work.
