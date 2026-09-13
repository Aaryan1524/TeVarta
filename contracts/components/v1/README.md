# Component contracts v1 — draft

`component_contracts.yaml` is a language-neutral typed contract catalogue for
the pipeline and graph boundaries. It is deliberately a draft until Aaryan and
Aaditya confirm it in the Friday review. It does not freeze the HTTP API; that
is T03.

Every caller uses a named operation or typed payload. Raw Cypher, an LLM-supplied
query, user identifiers in graph data, and unprovenanced edge writes are invalid
by contract.
