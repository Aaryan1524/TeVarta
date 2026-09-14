# T07 — SEC catalog import

**Owner:** Aaryan
**Branch:** `aaryan/phase-1/t07-sec-catalog`

The importer accepts a downloaded SEC `company_tickers.json`, creates stable
`ent_sec_<10-digit-cik>` catalog entities, and writes prefixed CIK/ticker plus name
aliases. Reimport updates SEC names/tickers but never changes `status`, so catalog
loading cannot promote an entity or expand the graph.

Run it with `PYTHONPATH=backend .venv/bin/python scripts/import_sec_catalog.py`
and an explicit downloaded file and database URL. The command emits a JSON import
report and never fetches SEC data itself.
