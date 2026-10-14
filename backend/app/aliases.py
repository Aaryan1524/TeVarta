"""T24 reviewed short names: the aliases news actually uses for catalog entities.

SEC titles make poor surface forms ("AMAZON COM INC" → `amazoncom`, "ASML HOLDING NV"
→ `asmlholdingnv`), so headlines saying "Amazon", "ASML", or "TSMC" would never tag
(§6.1: live entities are "deeply resolved: aliases"). `graph/entity_aliases.yaml` is
reviewed in PRs like code; this loader resolves each entity strictly and refuses an
alias that another entity already owns unless the entry says it is shared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yaml

from app.entity_resolution import identifier_alias
from app.sec_catalog import CatalogInputError, normalize_alias

DEFAULT_ALIASES_PATH = Path(__file__).resolve().parents[2] / "graph" / "entity_aliases.yaml"
ALIAS_SOURCE = "reviewed_alias"
ALIAS_CONFIDENCE = 0.9


class AliasFileError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)


@dataclass(frozen=True)
class AliasEntry:
    entity: str
    names: tuple[str, ...]
    rationale: str
    shared: bool


def parse_alias_file(payload: Any) -> tuple[AliasEntry, ...]:
    errors: list[str] = []
    if not isinstance(payload, Mapping) or not isinstance(payload.get("aliases"), list):
        raise AliasFileError(["file must contain an `aliases` list"])
    entries = []
    for index, raw in enumerate(payload["aliases"]):
        where = f"aliases[{index}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{where}: must be a mapping")
            continue
        unknown = set(raw) - {"entity", "names", "rationale", "shared"}
        if unknown:
            errors.append(f"{where}: unknown fields {sorted(unknown)}")
        entity, names, rationale = raw.get("entity"), raw.get("names"), raw.get("rationale")
        if not isinstance(entity, str) or not entity.strip():
            errors.append(f"{where}.entity: required")
            continue
        if not isinstance(rationale, str) or not rationale.strip():
            errors.append(f"{where}.rationale: required")
        if not isinstance(names, list) or not names:
            errors.append(f"{where}.names: at least one name")
            continue
        for name in names:
            try:
                if not isinstance(name, str):
                    raise CatalogInputError("not text")
                normalize_alias(name)
            except CatalogInputError:
                errors.append(f"{where}.names: {name!r} normalizes to nothing")
        entries.append(AliasEntry(entity.strip(), tuple(n for n in names if isinstance(n, str)),
                                  str(rationale or ""), bool(raw.get("shared", False))))
    seen: dict[str, str] = {}
    for entry in entries:
        for name in entry.names:
            try:
                key = normalize_alias(name)
            except CatalogInputError:
                continue
            if seen.setdefault(key, entry.entity) != entry.entity:
                errors.append(f"{name!r} is listed for both {seen[key]} and {entry.entity}")
    if errors:
        raise AliasFileError(errors)
    return tuple(entries)


def read_alias_file(path: Path = DEFAULT_ALIASES_PATH) -> tuple[AliasEntry, ...]:
    return parse_alias_file(yaml.safe_load(path.read_text(encoding="utf-8")))


def resolve_entity(connection: psycopg.Connection[Any], reference: str) -> str:
    """Strict: an entity_id, a ticker, or a CIK; never a name or seed_count guess."""
    if reference.startswith("ent_"):
        row = connection.execute(
            "SELECT entity_id FROM entities WHERE entity_id = %s", (reference,)
        ).fetchone()
    else:
        key = identifier_alias(reference)
        row = connection.execute(
            "SELECT entity_id FROM entity_aliases WHERE alias_norm = %s AND is_identifier",
            (key,),
        ).fetchone() if key else None
    if row is None:
        raise LookupError(f"{reference!r} does not resolve to a catalog entity")
    return str(row[0])


@dataclass(frozen=True)
class AliasLoadReport:
    entries: int
    added: int
    present: int

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


def apply_aliases(connection: psycopg.Connection[Any],
                  entries: Sequence[AliasEntry]) -> AliasLoadReport:
    """Validate every entry against the catalog, then write all aliases or none."""
    errors: list[str] = []
    planned: list[tuple[str, str]] = []
    with connection.transaction():
        for entry in entries:
            try:
                entity_id = resolve_entity(connection, entry.entity)
            except LookupError as error:
                errors.append(str(error))
                continue
            for name in entry.names:
                key = normalize_alias(name)
                owners = {row[0] for row in connection.execute(
                    "SELECT entity_id FROM entity_aliases WHERE alias_norm = %s", (key,)
                ).fetchall()} - {entity_id}
                if owners and not entry.shared:
                    errors.append(f"{name!r} ({key}) already names {sorted(owners)}; "
                                  "mark the entry `shared: true` only if both are intended")
                    continue
                planned.append((key, entity_id))
        if errors:
            raise AliasFileError(errors)
        added = 0
        for key, entity_id in planned:
            added += connection.execute(
                """INSERT INTO entity_aliases (alias_norm, entity_id, source, confidence,
                                               is_identifier)
                   VALUES (%s, %s, %s, %s, false)
                   ON CONFLICT (alias_norm, entity_id) DO NOTHING""",
                (key, entity_id, ALIAS_SOURCE, ALIAS_CONFIDENCE),
            ).rowcount
    return AliasLoadReport(len(entries), added, len(planned) - added)
