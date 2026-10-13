# T08 — Entity resolution rungs 1–3

**Owner:** Aaryan
**Branch:** `aaryan/phase-1/t08-entity-resolution`

The resolver handles CIK, ticker, exchange-qualified ticker, and normalized exact
aliases only. Multi-candidate names are ranked first by provided article-context
co-occurrence and then by `seed_count`; a remaining tie returns candidates with no
tag and writes `resolution_misses`. Fuzzy, embedding, and LLM resolution remain
out of scope until the frozen Week 10 work.

Rung 1 has two modes. A deliberate identifier query (onboarding search, curation)
treats a bare all-caps symbol or a raw CIK as an identifier. Article tagging (T20)
must call `resolve(..., bare_identifiers=False)`. In that mode, headline words such
as "AI", "IT", or "ON" and numbers such as "2026" fall through to exact-alias
matching. Only explicit syntax is an identifier: `$AAPL`, a known exchange prefix
(`NASDAQ: AAPL`), an exchange suffix (`2330.TW`, `AAPL.O`), or `CIK 320193`.
Share classes written `BRK.B`, `BRK/B`, or `BRK-B` normalize to SEC's `BRK-B`.
