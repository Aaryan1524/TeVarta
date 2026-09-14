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


def identifier_alias(surface: str) -> str | None:
    """Convert CIK, ticker, and exchange-qualified symbols to T07 alias keys."""
    value = surface.strip()
    cik = re.fullmatch(r"(?:cik\s*[:#-]?\s*)?(\d{1,10})", value, flags=re.IGNORECASE)
    if cik:
        return f"cik:{cik.group(1).zfill(10)}"
    exchange = re.fullmatch(
        r"(?:[A-Za-z]+\s*:\s*)?([A-Za-z0-9]{1,10})(?:\.[A-Za-z]{2,4})?", value
    )
    qualified = ":" in value or "." in value
    if exchange and (qualified or value.isupper()):
        return f"ticker:{exchange.group(1).casefold()}"
    return None


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


def alias_key(surface: str) -> tuple[str, int]:
    identifier = identifier_alias(surface)
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
    ) -> Resolution:
        key, rung = alias_key(surface_form)
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
