"""Repeatable, database-only import of the SEC company ticker catalog (T07)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import psycopg


class CatalogInputError(ValueError):
    """Raised when a company_tickers.json payload cannot establish stable identity."""


@dataclass(frozen=True)
class CompanyTicker:
    cik: str
    ticker: str
    canonical_name: str

    @property
    def entity_id(self) -> str:
        return f"ent_sec_{self.cik}"


@dataclass(frozen=True)
class CatalogImportReport:
    companies: int
    entity_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_alias(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    if not normalized:
        raise CatalogInputError("company title must be non-empty")
    return normalized


def parse_company_tickers(payload: dict[str, Any]) -> tuple[CompanyTicker, ...]:
    """Parse SEC's numbered object while rejecting ambiguous identities."""
    companies: dict[str, CompanyTicker] = {}
    for raw in payload.values():
        if not isinstance(raw, dict):
            raise CatalogInputError("each SEC catalog entry must be an object")
        try:
            cik = str(int(raw["cik_str"])).zfill(10)
            ticker = str(raw["ticker"]).strip().upper()
            title = str(raw["title"]).strip()
        except (KeyError, TypeError, ValueError) as error:
            message = "each SEC catalog entry needs cik_str, ticker, and title"
            raise CatalogInputError(message) from error
        if not ticker or not title:
            raise CatalogInputError("ticker and title must be non-empty")
        company = CompanyTicker(cik=cik, ticker=ticker, canonical_name=title)
        existing = companies.get(cik)
        if existing and existing != company:
            raise CatalogInputError(f"conflicting SEC entries for CIK {cik}")
        companies[cik] = company
    if not companies:
        raise CatalogInputError("SEC catalog cannot be empty")
    return tuple(companies[cik] for cik in sorted(companies))


def import_sec_catalog(
    connection: psycopg.Connection[Any], companies: tuple[CompanyTicker, ...]
) -> CatalogImportReport:
    """Upsert catalog entities and aliases without changing live/graph state."""
    with connection.transaction():
        for company in companies:
            connection.execute(
                """INSERT INTO entities (entity_id, canonical_name, type, ticker, cik, status)
                   VALUES (%s, %s, 'Company', %s, %s, 'catalog')
                   ON CONFLICT (entity_id) DO UPDATE SET canonical_name = EXCLUDED.canonical_name,
                     ticker = EXCLUDED.ticker, cik = EXCLUDED.cik""",
                (company.entity_id, company.canonical_name, company.ticker, company.cik),
            )
            aliases = (
                (f"cik:{company.cik}", True),
                (f"ticker:{company.ticker.casefold()}", True),
                (normalize_alias(company.canonical_name), False),
            )
            for alias, identifier in aliases:
                connection.execute(
                    """INSERT INTO entity_aliases
                       (alias_norm, entity_id, source, confidence, is_identifier)
                       VALUES (%s, %s, 'sec_company_tickers', 1.0, %s)
                       ON CONFLICT (alias_norm, entity_id) DO UPDATE SET
                         confidence = EXCLUDED.confidence,
                         is_identifier = EXCLUDED.is_identifier""",
                    (alias, company.entity_id, identifier),
                )
    return CatalogImportReport(len(companies), tuple(company.entity_id for company in companies))
