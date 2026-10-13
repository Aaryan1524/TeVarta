"""Deterministic entity-resolution rungs 1–3 from frozen MVP §6.4."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg

from app.sec_catalog import normalize_alias


@dataclass(frozen=True)
class ResolutionCandidate:
    entity_id: str
    canonical_name: str
    seed_count: int


@dataclass(frozen=True)
class Resolution:
    status: str
    entity_id: str | None
    candidates: tuple[ResolutionCandidate, ...]
    rung: int | None


EXCHANGE_PREFIXES = frozenset(
    {"NYSE", "NASDAQ", "NYSEARCA", "NYSEAMERICAN", "AMEX", "OTC", "OTCMKTS", "CBOE", "BATS",
     "TSX", "TSXV", "LSE", "HKEX", "TWSE", "TPE", "KRX", "SGX", "ASX", "XETRA", "EURONEXT"}
)
# Venue suffixes as feeds write them (2330.TW, VOD.L, AAPL.O). An unknown suffix is
# not evidence of a listing: "U.S" or "e.g" must never become a ticker.
EXCHANGE_SUFFIXES = frozenset(
    {"TW", "TWO", "T", "HK", "L", "PA", "DE", "F", "AS", "SW", "TO", "V", "AX", "KS", "KQ",
     "SS", "SZ", "NS", "BO", "SI", "MI", "MC", "ST", "OL", "CO", "HE", "BR", "LS", "SA", "MX",
     "O", "OQ", "N"}
)
# SEC writes share classes with a hyphen (BRK-B); news writes BRK.B or BRK/B.
SHARE_CLASSES = frozenset("ABC")
_CIK = re.compile(r"(cik\s*[:#-]?\s*)?(\d{1,10})", flags=re.IGNORECASE)
_SYMBOL = re.compile(
    r"(?:(?P<cashtag>\$)|(?P<exchange>[A-Za-z]+)\s*:\s*)?"
    r"(?P<symbol>[A-Za-z0-9]{1,10})"
    r"(?:(?P<separator>[.\-/])(?P<suffix>[A-Za-z]{1,4}))?"
)


def identifier_alias(surface: str, *, bare: bool = True) -> str | None:
    """Convert CIK, ticker, and exchange-qualified symbols to T07 alias keys.

    `bare=True` suits a deliberate identifier query, where `SNDK` or a raw CIK means
    what it says. Article text must pass `bare=False`: a headline's "AI", "IT", or
    "2026" is not a ticker or CIK unless written as `$AAPL`, `NASDAQ: AAPL`,
    `2330.TW`, or `CIK 320193`.
    """
    value = surface.strip()
    cik = _CIK.fullmatch(value)
    if cik:
        return f"cik:{cik.group(2).zfill(10)}" if bare or cik.group(1) else None
    match = _SYMBOL.fullmatch(value)
    if not match:
        return None
    exchange = match.group("exchange")
    if exchange and exchange.upper() not in EXCHANGE_PREFIXES:
        return None
    symbol, separator, suffix = match.group("symbol", "separator", "suffix")
    share_class = venue = None
    if suffix:
        if len(symbol) == 1 and len(suffix) == 1:
            return None  # "U.S", "A.I", "U.N": abbreviations, not listings
        if len(suffix) == 1 and suffix.upper() in SHARE_CLASSES:
            share_class = suffix
        elif (
            separator == "."
            and suffix.upper() in EXCHANGE_SUFFIXES
            and (bare or f"{symbol}{suffix}".isupper() or (symbol.isdigit() and suffix.isupper()))
        ):
            venue = suffix
        else:
            return None
    explicit = bool(match.group("cashtag") or exchange or venue)
    if not (explicit or (bare and (value.isupper() or share_class))):
        return None
    ticker = f"{symbol}-{share_class}" if share_class else symbol
    return f"ticker:{ticker.casefold()}"


def choose_candidate(
    candidates: Iterable[ResolutionCandidate], context_cooccurrence: dict[str, int]
) -> Resolution:
    """Apply the frozen co-occurrence then seed-count tie-break, never guessing."""
    options = tuple(candidates)
    if not options:
        return Resolution("unresolved", None, (), None)
    if len(options) == 1:
        return Resolution("resolved", options[0].entity_id, options, None)
    ranked = sorted(
        options,
        key=lambda candidate: (
            context_cooccurrence.get(candidate.entity_id, 0),
            candidate.seed_count,
        ),
        reverse=True,
    )
    top = ranked[0]
    if len(ranked) > 1 and (
        context_cooccurrence.get(top.entity_id, 0), top.seed_count
    ) == (
        context_cooccurrence.get(ranked[1].entity_id, 0), ranked[1].seed_count
    ):
        return Resolution("ambiguous", None, tuple(ranked), None)
    return Resolution("resolved", top.entity_id, tuple(ranked), None)


def alias_key(surface: str, *, bare: bool = True) -> tuple[str, int]:
    identifier = identifier_alias(surface, bare=bare)
    if identifier:
        return identifier, 1
    return normalize_alias(surface), 2


class EntityResolver:
    """Postgres boundary for frozen rungs 1–3 and auditable unresolved aliases."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def resolve(
        self,
        surface_form: str,
        *,
        context_cooccurrence: dict[str, int] | None = None,
        article_id: str | None = None,
        bare_identifiers: bool = True,
    ) -> Resolution:
        """Resolve one surface form; article tagging must pass bare_identifiers=False."""
        key, rung = alias_key(surface_form, bare=bare_identifiers)
        rows = self._connection.execute(
            """SELECT entities.entity_id, entities.canonical_name, entities.seed_count
               FROM entity_aliases JOIN entities USING (entity_id)
               WHERE entity_aliases.alias_norm = %s
               ORDER BY entities.entity_id""",
            (key,),
        ).fetchall()
        candidates = tuple(ResolutionCandidate(*row) for row in rows)
        chosen = choose_candidate(candidates, context_cooccurrence or {})
        result = Resolution(chosen.status, chosen.entity_id, chosen.candidates, rung)
        if result.entity_id is None:
            self._log_miss(surface_form, context_cooccurrence or {}, article_id)
        return result

    def _log_miss(
        self, surface_form: str, context_cooccurrence: dict[str, int], article_id: str | None
    ) -> None:
        self._connection.execute(
            """INSERT INTO resolution_misses (surface_form, context, article_id)
               VALUES (%s, %s, %s)""",
            (surface_form, json.dumps(context_cooccurrence, sort_keys=True), article_id),
        )
