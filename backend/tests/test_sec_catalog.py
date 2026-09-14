import json
from pathlib import Path

import pytest
from app.sec_catalog import CatalogInputError, normalize_alias, parse_company_tickers

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/sec/v1/company_tickers.json"


def test_sec_payload_creates_stable_catalog_ids_and_identifier_aliases() -> None:
    companies = parse_company_tickers(json.loads(FIXTURE.read_text()))
    assert [(company.entity_id, company.cik, company.ticker) for company in companies] == [
        ("ent_sec_0000320193", "0000320193", "AAPL"),
        ("ent_sec_0001000184", "0001000184", "SNDK"),
    ]
    assert normalize_alias("  Apple   Inc. ") == "apple inc."


@pytest.mark.parametrize(
    "payload", [{}, {"0": {}}, {"0": {"cik_str": 1, "ticker": "", "title": "X"}}]
)
def test_sec_payload_rejects_missing_or_unstable_identity(payload: dict[str, object]) -> None:
    with pytest.raises(CatalogInputError):
        parse_company_tickers(payload)
