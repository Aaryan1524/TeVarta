# T09 — Deterministic scoring primitives

**Owner:** Aaryan
**Branch:** `aaryan/phase-1/t09-scoring-primitives`
**Dependencies:** T01 execution inputs, T04 component contracts, and the frozen MVP §9.

## Outcome

Provide a dependency-free Python module for deterministic confidence, path score,
importance, freshness, relevance, and the confidence notification gate. It accepts
only observable numeric inputs; it performs no model call, graph traversal, source
fetch, or persistence.

The upstream evidence aggregator owns two inputs that are intentionally outside
these primitives: it must count **distinct originating publisher groups** (never
domains or article volume), and map the frozen directness/resolution categories to
their fixed values before invoking `confidence`.

## Done means

- §9.1 confidence uses publisher groups (not domains) and passes all three frozen
  sanity cases.
- §9.2 scores paths with the four explicit hop factors and a three-hop maximum;
  its 0.05 floor controls expansion without raising a weak path's score.
- §9.3 keeps source quality as the gate on corroboration, so volume cannot create
  importance.
- §9.4 retains all seven measured relevance components while shipping only the
  three frozen additive weights.
- §9.5 treats path confidence of exactly 0.6 as eligible for the confidence gate.
- Invalid, non-finite, and mismatched path inputs fail deterministically.

## Verification

`make backend-test` covers frozen examples, boundary behavior, input rejection,
and `fixtures/scoring/v1/golden_cases.json` preserves exact pre-display precision
behind MVP table values.
