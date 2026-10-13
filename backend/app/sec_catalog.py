"""Repeatable, database-only import of the SEC company ticker catalog (T07)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import psycopg


class CatalogInputError(ValueError):
    """Raised when a company_tickers.json payload cannot establish stable identity."""


@dataclass(frozen=True)
class CompanyTicker:
    cik: str
    ticker: str  # primary: the first listing SEC gives for this CIK
    canonical_name: str
    # Every share class SEC lists for the CIK (GOOGL and GOOG), primary first.
    tickers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.tickers:
            object.__setattr__(self, "tickers", (self.ticker,))
        if self.tickers[0] != self.ticker or len(set(self.tickers)) != len(self.tickers):
            raise CatalogInputError(f"invalid ticker list for CIK {self.cik}")

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
    normalized = value.casefold().strip()
    normalized = re.sub(
        r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc)\b", "", normalized
    )
    normalized = re.sub(r"[^a-z0-9]+", "", normalized)
    if not normalized:
        raise CatalogInputError("company title must be non-empty")
    return normalized


def parse_company_tickers(payload: dict[str, Any]) -> tuple[CompanyTicker, ...]:
    """Parse SEC's numbered object while rejecting ambiguous identities.

    SEC lists one row per ticker, so a multi-class issuer appears once per share
    class under the same CIK. Those rows collapse into one company with every
    ticker; a CIK with two titles, or a ticker claimed by two CIKs, is rejected.
    """
    titles: dict[str, str] = {}
    tickers: dict[str, list[str]] = {}
    ticker_owner: dict[str, str] = {}
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
        if titles.setdefault(cik, title) != title:
            raise CatalogInputError(f"conflicting SEC titles for CIK {cik}")
        owner = ticker_owner.setdefault(ticker, cik)
        if owner != cik:
            raise CatalogInputError(f"ticker {ticker} is listed for CIKs {owner} and {cik}")
        cik_tickers = tickers.setdefault(cik, [])
        if ticker not in cik_tickers:
            cik_tickers.append(ticker)
    if not titles:
        raise CatalogInputError("SEC catalog cannot be empty")
    return tuple(
        CompanyTicker(
            cik=cik,
            ticker=tickers[cik][0],
            canonical_name=titles[cik],
            tickers=tuple(tickers[cik]),
        )
        for cik in sorted(titles)
    )


def import_sec_catalog(
    connection: psycopg.Connection[Any], companies: tuple[CompanyTicker, ...]
) -> CatalogImportReport:
    """Upsert catalog entities and aliases without changing live/graph state."""
    with connection.transaction():
        # Retire ticker aliases a company no longer lists before inserting any, so a
        # ticker that moved between CIKs never collides with the identifier index.
        for company in companies:
            connection.execute(
                """DELETE FROM entity_aliases
                   WHERE entity_id = %s AND source = 'sec_company_tickers'
                     AND is_identifier AND alias_norm LIKE 'ticker:%%'
                     AND NOT (alias_norm = ANY(%s))""",
                (company.entity_id, [f"ticker:{ticker.casefold()}" for ticker in company.tickers]),
            )
        owners = {
            f"ticker:{ticker.casefold()}": company.entity_id
            for company in companies
            for ticker in company.tickers
        }
        # A ticker whose previous CIK is absent from this file moves to its new owner.
        connection.execute(
            """DELETE FROM entity_aliases AS alias
               USING unnest(%s::text[], %s::text[]) AS owner(alias_norm, entity_id)
               WHERE alias.source = 'sec_company_tickers' AND alias.is_identifier
                 AND alias.alias_norm = owner.alias_norm
                 AND alias.entity_id <> owner.entity_id""",
            (list(owners), list(owners.values())),
        )
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
                *((f"ticker:{ticker.casefold()}", True) for ticker in company.tickers),
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
