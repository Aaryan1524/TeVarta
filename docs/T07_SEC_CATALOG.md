# T07 — SEC catalog import

**Owner:** Aaryan
**Branch:** `aaryan/phase-1/t07-sec-catalog`

The importer accepts a downloaded SEC `company_tickers.json`, creates stable
`ent_sec_<10-digit-cik>` catalog entities, and writes prefixed CIK/ticker plus name
aliases. Reimport updates SEC names/tickers but never changes `status`, so catalog
loading cannot promote an entity or expand the graph.

SEC lists one row per ticker, so a multi-class issuer (GOOGL/GOOG, BRK-A/BRK-B)
appears several times under one CIK. Those rows become one entity: the first
ticker in SEC's order is `entities.ticker`, and every ticker is an identifier
alias. A CIK with two titles, or a ticker claimed by two CIKs, fails the import.
On reimport, SEC ticker aliases a company no longer lists, or that moved to
another CIK, are retired before new ones are written, so the one-owner identifier
index never blocks a reassigned ticker.

Run it with `PYTHONPATH=backend .venv/bin/python scripts/import_sec_catalog.py`
and an explicit downloaded file and database URL. The command emits a JSON import
report and never fetches SEC data itself.
