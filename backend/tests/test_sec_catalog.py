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
    assert normalize_alias("  Apple   Inc. ") == "apple"


def test_multi_class_issuer_keeps_every_ticker_with_sec_order_primary() -> None:
    companies = parse_company_tickers(
        {
            "0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
            "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "2": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
            "3": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
        }
    )

    alphabet = companies[1]
    assert alphabet.entity_id == "ent_sec_0001652044"
    assert alphabet.ticker == "GOOGL"
    assert alphabet.tickers == ("GOOGL", "GOOG")
    assert companies[0].tickers == ("AAPL",)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "0": {"cik_str": 1, "ticker": "AAA", "title": "One Inc."},
                "1": {"cik_str": 1, "ticker": "AAB", "title": "Other Inc."},
            },
            "conflicting SEC titles",
        ),
        (
            {
                "0": {"cik_str": 1, "ticker": "AAA", "title": "One Inc."},
                "1": {"cik_str": 2, "ticker": "AAA", "title": "Two Inc."},
            },
            "listed for CIKs",
        ),
    ],
)
def test_ambiguous_titles_or_shared_tickers_are_rejected(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(CatalogInputError, match=message):
        parse_company_tickers(payload)


@pytest.mark.parametrize(
    "payload", [{}, {"0": {}}, {"0": {"cik_str": 1, "ticker": "", "title": "X"}}]
)
def test_sec_payload_rejects_missing_or_unstable_identity(payload: dict[str, object]) -> None:
    with pytest.raises(CatalogInputError):
        parse_company_tickers(payload)
