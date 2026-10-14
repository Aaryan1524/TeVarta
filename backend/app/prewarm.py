"""T24 pre-warm: promote likely early seeds through the T23 expansion machinery.

Idempotent: re-running re-expands the same entities and republishes their views.
The coverage report states gaps (an entity with no dependency edges yet) instead
of inventing edges; curation (T15) closes them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yaml

from app.aliases import resolve_entity
from app.expansion import EntityExpander, dispatch, request_expansion

DEFAULT_PREWARM_PATH = Path(__file__).resolve().parents[2] / "config" / "prewarm.yaml"


def read_prewarm_list(path: Path = DEFAULT_PREWARM_PATH) -> tuple[str, ...]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    entities = payload.get("entities") if isinstance(payload, dict) else None
    if not isinstance(entities, list) or not all(isinstance(e, str) for e in entities):
        raise ValueError("pre-warm file must contain an `entities` list of references")
    if len(set(entities)) != len(entities):
        raise ValueError("pre-warm list has duplicates")
    return tuple(entities)


@dataclass(frozen=True)
class Coverage:
    reference: str
    entity_id: str
    name: str
    live: bool
    view: str  # 'ready' | 'failed' | 'missing'
    upstream_entities: int
    dependency_edges: int  # edges other than IN_INDUSTRY

    @property
    def gap(self) -> bool:
        return self.view != "ready" or self.dependency_edges == 0


def prewarm(connection: psycopg.Connection[Any], expander: EntityExpander,
            references: Sequence[str]) -> list[Coverage]:
    ids = {reference: resolve_entity(connection, reference) for reference in references}
    connection.commit()
    request_expansion(connection, list(ids.values()))
    while dispatch(connection, expander, limit=10):
        pass
    return coverage(connection, ids)


def coverage(connection: psycopg.Connection[Any], ids: dict[str, str]) -> list[Coverage]:
    rows = connection.execute(
        """SELECT e.entity_id, e.canonical_name, e.status = 'live', v.status, v.graph_json
           FROM entities e LEFT JOIN entity_graph_views v USING (entity_id)
           WHERE e.entity_id = ANY(%s)""",
        (list(ids.values()),),
    ).fetchall()
    by_id = {row[0]: row for row in rows}
    report = []
    for reference, entity_id in ids.items():
        _, name, live, view_status, view = by_id[entity_id]
        edges = (view or {}).get("edges", [])
        report.append(Coverage(
            reference, entity_id, name, bool(live), view_status or "missing",
            max(0, len((view or {}).get("nodes", [])) - 1),
            sum(1 for edge in edges if edge["relation"] != "IN_INDUSTRY"),
        ))
    return report
