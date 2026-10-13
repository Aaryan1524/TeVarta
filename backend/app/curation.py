"""Reviewed curated-edge workflow (T14, frozen MVP §6.2).

Curated edges are hand-accepted World Graph edges. This module validates the
version-controlled YAML, records every accept/edit/reject decision in an
append-only review log, and loads only edges whose exact content hash appears in
an accepting review record. Confidence is always computed from the recorded
evidence (§9.1); a curator can never supply it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import psycopg
import yaml

from app.entity_resolution import alias_key
from app.graph_store import (
    CURATED_ORIGIN,
    ENTITY_TYPES,
    RELATION_TYPES,
    GraphStore,
    GraphWriteError,
    TemporalEdgeRevision,
)
from app.scoring import confidence
from app.sec_catalog import CatalogInputError, normalize_alias

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EDGES_PATH = REPOSITORY_ROOT / "graph" / "curated_edges.yaml"
DEFAULT_REVIEW_LOG_PATH = REPOSITORY_ROOT / "graph" / "curation" / "review_log.jsonl"

EDGE_SCHEMA = "tevarta.curated_edge.v1"
REVIEW_SCHEMA = "tevarta.curation_review.v1"
# Deterministic namespace: one reviewed curation SHA always maps to one graph request ID.
CURATION_REQUEST_NAMESPACE = UUID("6f0c5d1e-3b8a-5c47-9d2e-7a41c0b9e8f3")
CURATORS = frozenset({"aaryan", "aaditya"})
# §9.3 primary-source strength, keyed by the closed evidence vocabulary.
SOURCE_TYPE_TIERS = MappingProxyType(
    {
        "sec_filing": 1.0,
        "regulator": 1.0,
        "exchange": 1.0,
        "central_bank": 1.0,
        "court": 1.0,
        "company_ir": 0.9,
        "major_wire": 0.7,
        "quality_secondary": 0.5,
        "aggregator": 0.25,
    }
)
# §9.1 directness: 1.0 stated, 0.6 inferred.
DIRECTNESS_VALUES = MappingProxyType({"stated": 1.0, "inferred": 0.6})
# §9.1 resolution quality for the rungs curation permits.
IDENTIFIER_RESOLUTION_QUALITY = 1.0
ALIAS_RESOLUTION_QUALITY = 0.7
# Proposed T01 relation defaults; drafts prefill them only for the reviewer to confirm.
RELATION_DEFAULT_WEIGHTS = MappingProxyType(
    {
        "DEPENDS_ON": 0.90,
        "SUPPLIES": 0.85,
        "SOLE_SOURCE_FOR": 0.95,
        "MANUFACTURES_AT": 0.85,
        "EXPOSED_TO": 0.80,
        "CUSTOMER_OF": 0.65,
        "COMPETES_WITH": 0.25,
        "PRODUCES": 0.75,
        "SUBSIDIARY_OF": 0.95,
        "OPERATED_BY": 0.90,
        "OPERATES_IN": 0.75,
        "REGULATED_BY": 0.65,
        "LISTED_AS": 0.99,
        "IN_INDUSTRY": 0.20,
    }
)

EDGE_FIELDS = (
    "source",
    "relation",
    "target",
    "weight",
    "directness",
    "rationale",
    "evidence",
    "valid_from",
    "curated_by",
    "curated_at",
    "review_by",
    "proposal_id",
    "drafted_by",
)
EVIDENCE_FIELDS = ("url", "source_type", "publisher_group", "locator")
ENTITY_FIELDS = ("entity_id", "canonical_name", "type", "aliases")
PROPOSAL_EDGE_FIELDS = (
    "source",
    "relation",
    "target",
    "weight",
    "directness",
    "rationale",
    "evidence",
    "valid_from",
    "review_by",
)
DRAFT_FIELDS = ("source", "relation", "target", "directness", "rationale", "excerpt")
DRAFT_COMPARED_FIELDS = ("source", "relation", "target", "rationale")
BATCH_FIELDS = (
    "batch_id",
    "drafted_by",
    "drafted_at",
    "usage_request_id",
    "subject",
    "evidence",
    "proposals",
)
PROPOSAL_FIELDS = ("proposal_id", "draft", "draft_sha", "decision", "reason", "edge")
REVIEW_FIELDS = (
    "schema",
    "proposal_id",
    "batch_id",
    "decision",
    "reviewer",
    "decided_at",
    "drafted_by",
    "usage_request_id",
    "draft",
    "draft_sha",
    "proposed_edge",
    "edge",
    "curated_sha",
    "reason",
)

CURATED_ENTITY_ID = re.compile(r"ent_cur_[a-z0-9]+(?:_[a-z0-9]+)*")
ENTITY_ID = re.compile(r"ent_[a-z0-9_]+")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,79}")
PUBLISHER_GROUP = re.compile(r"[a-z0-9][a-z0-9_]{1,63}")
DRAFTED_BY = re.compile(r"human|llm:[A-Za-z0-9._:/-]{1,100}")
PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net", "localhost")
PLACEHOLDER_SUFFIXES = (".example", ".invalid", ".test", ".localhost")
MAX_RATIONALE_CHARS = 500
MAX_TEXT_CHARS = 200

EDGES_FILE_HEADER = """\
# Reviewed curated World Graph edges (frozen MVP §6.2); see docs/T14_CURATION_WORKFLOW.md.
# Declare non-SEC entities by hand. Add edges only via `scripts/curate_edges.py review`,
# which re-renders this file: each edge must match an accept/edit record in
# graph/curation/review_log.jsonl by content SHA or the loader refuses it.
# Confidence is computed from evidence (§9.1) and is never written here.
"""


class CurationValidationError(ValueError):
    """Every problem found in one validation pass; nothing is written when raised."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Evidence:
    url: str
    source_type: str
    publisher_group: str
    locator: str | None

    @property
    def tier_value(self) -> float:
        return SOURCE_TYPE_TIERS[self.source_type]

    def canonical(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "source_type": self.source_type,
            "publisher_group": self.publisher_group,
            "locator": self.locator,
        }


@dataclass(frozen=True)
class CuratedEntity:
    entity_id: str
    canonical_name: str
    entity_type: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class CuratedEdge:
    source: str
    relation: str
    target: str
    weight: float
    directness: float
    rationale: str
    evidence: tuple[Evidence, ...]
    valid_from: date
    curated_by: str
    curated_at: date
    review_by: date
    proposal_id: str
    drafted_by: str

    def canonical(self) -> dict[str, Any]:
        """The exact reviewed content; its hash is the edge's `curated:<sha>` provenance."""
        return {
            "schema": EDGE_SCHEMA,
            "source": self.source,
            "relation": self.relation,
            "target": self.target,
            "weight": self.weight,
            "directness": self.directness,
            "rationale": self.rationale,
            "evidence": [item.canonical() for item in self.evidence],
            "valid_from": self.valid_from.isoformat(),
            "curated_by": self.curated_by,
            "curated_at": self.curated_at.isoformat(),
            "review_by": self.review_by.isoformat(),
            "proposal_id": self.proposal_id,
            "drafted_by": self.drafted_by,
        }

    @property
    def sha(self) -> str:
        return content_sha(self.canonical())

    @property
    def source_id(self) -> str:
        return f"curated:{self.sha}"

    @property
    def surface_key(self) -> tuple[str, str, str]:
        return (self.source.casefold(), self.relation, self.target.casefold())


@dataclass(frozen=True)
class CurationFile:
    entities: tuple[CuratedEntity, ...]
    edges: tuple[CuratedEdge, ...]


@dataclass(frozen=True)
class ReviewRecord:
    proposal_id: str
    batch_id: str
    decision: str
    reviewer: str
    decided_at: date
    drafted_by: str
    usage_request_id: str | None
    draft: dict[str, Any] | None
    draft_sha: str | None
    proposed_edge: dict[str, Any] | None
    edge: dict[str, Any] | None
    curated_sha: str | None
    reason: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": REVIEW_SCHEMA,
            "proposal_id": self.proposal_id,
            "batch_id": self.batch_id,
            "decision": self.decision,
            "reviewer": self.reviewer,
            "decided_at": self.decided_at.isoformat(),
            "drafted_by": self.drafted_by,
            "usage_request_id": self.usage_request_id,
            "draft": self.draft,
            "draft_sha": self.draft_sha,
            "proposed_edge": self.proposed_edge,
            "edge": self.edge,
            "curated_sha": self.curated_sha,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReviewLog:
    records: tuple[ReviewRecord, ...]

    @property
    def decided_proposal_ids(self) -> frozenset[str]:
        return frozenset(record.proposal_id for record in self.records)

    @property
    def accepted_shas(self) -> frozenset[str]:
        return frozenset(
            record.curated_sha
            for record in self.records
            if record.decision in {"accepted", "edited"} and record.curated_sha
        )

    def find_sha(self, sha: str) -> ReviewRecord | None:
        return next((record for record in self.records if record.curated_sha == sha), None)


def content_sha(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode()).hexdigest()


def curation_request_id(sha: str) -> UUID:
    return uuid5(CURATION_REQUEST_NAMESPACE, f"curated:{sha}")


def utc_today() -> date:
    return datetime.now(UTC).date()


# ---------------------------------------------------------------------------
# Field parsing: every helper appends to `errors` and returns None on failure.


def _mapping(value: Any, where: str, fields: Sequence[str], errors: list[str]) -> bool:
    if not isinstance(value, Mapping):
        errors.append(f"{where}: must be a mapping")
        return False
    if "confidence" in value:
        errors.append(f"{where}.confidence: confidence is computed from evidence (§9.1)")
    unknown = sorted(str(key) for key in value if key not in fields and key != "confidence")
    missing = [field for field in fields if field not in value]
    if unknown:
        errors.append(f"{where}: unknown fields {unknown}")
    if missing:
        errors.append(f"{where}: missing required fields {missing}")
    return not missing


def _text(
    value: Any, where: str, errors: list[str], limit: int = MAX_TEXT_CHARS
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: must be a non-empty string")
        return None
    if value != value.strip() or len(value) > limit:
        errors.append(f"{where}: must be trimmed and at most {limit} characters")
        return None
    return value


def _pattern(value: Any, where: str, pattern: re.Pattern[str], errors: list[str]) -> str | None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        errors.append(f"{where}: must match {pattern.pattern}")
        return None
    return value


def _unit(value: Any, where: str, errors: list[str]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        errors.append(f"{where}: must be a number in (0, 1]")
        return None
    return float(value)


def _day(value: Any, where: str, errors: list[str]) -> date | None:
    if isinstance(value, datetime):
        errors.append(f"{where}: must be a date (YYYY-MM-DD), not a timestamp")
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    errors.append(f"{where}: must be a date (YYYY-MM-DD)")
    return None


def _evidence_url(value: Any, where: str, errors: list[str]) -> str | None:
    url = _text(value, where, errors, limit=2000)
    if url is None:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if parts.scheme != "https" or not host or " " in url:
        errors.append(f"{where}: evidence must be an https URL")
        return None
    if (
        host in PLACEHOLDER_HOSTS
        or any(host.endswith(f".{placeholder}") for placeholder in PLACEHOLDER_HOSTS)
        or host.endswith(PLACEHOLDER_SUFFIXES)
        or "." not in host
    ):
        errors.append(f"{where}: evidence must be a real public URL, not {host}")
        return None
    return url


def _parse_evidence(value: Any, where: str, errors: list[str]) -> tuple[Evidence, ...] | None:
    if not isinstance(value, list) or not value:
        errors.append(f"{where}: at least one evidence item is required")
        return None
    items: list[Evidence] = []
    seen: set[str] = set()
    before = len(errors)
    for index, raw in enumerate(value):
        item_where = f"{where}[{index}]"
        if isinstance(raw, Mapping):
            raw = {"locator": None, **raw}  # a locator is optional in the YAML
        if not _mapping(raw, item_where, EVIDENCE_FIELDS, errors):
            continue
        url = _evidence_url(raw["url"], f"{item_where}.url", errors)
        source_type = raw["source_type"]
        if source_type not in SOURCE_TYPE_TIERS:
            errors.append(
                f"{item_where}.source_type: must be one of {sorted(SOURCE_TYPE_TIERS)}"
            )
        group = _pattern(raw["publisher_group"], f"{item_where}.publisher_group",
                         PUBLISHER_GROUP, errors)
        locator = raw["locator"]
        if locator is not None:
            locator = _text(locator, f"{item_where}.locator", errors)
        if url is not None and url in seen:
            errors.append(f"{item_where}.url: duplicate evidence URL")
        if url is not None:
            seen.add(url)
        if url and group and source_type in SOURCE_TYPE_TIERS:
            items.append(Evidence(url, source_type, group, locator))
    return tuple(items) if len(errors) == before else None


def parse_evidence(value: Any, where: str = "evidence") -> tuple[Evidence, ...]:
    errors: list[str] = []
    evidence = _parse_evidence(value, where, errors)
    if evidence is None:
        raise CurationValidationError(errors)
    return evidence


def _endpoint(value: Any, where: str, errors: list[str]) -> str | None:
    endpoint = _text(value, where, errors)
    if endpoint is not None and endpoint.casefold().startswith("usr_"):
        errors.append(f"{where}: the World Graph never accepts user IDs")
        return None
    return endpoint


def parse_edge(raw: Any, where: str, today: date) -> CuratedEdge:
    """Validate one curated edge mapping, raising every problem at once."""
    errors: list[str] = []
    if not _mapping(raw, where, EDGE_FIELDS, errors):
        raise CurationValidationError(errors)
    source = _endpoint(raw["source"], f"{where}.source", errors)
    target = _endpoint(raw["target"], f"{where}.target", errors)
    relation = raw["relation"]
    if relation not in RELATION_TYPES:
        errors.append(f"{where}.relation: must be one of the 14 frozen relation types")
    weight = _unit(raw["weight"], f"{where}.weight", errors)
    directness = raw["directness"]
    if isinstance(directness, bool) or directness not in DIRECTNESS_VALUES.values():
        errors.append(f"{where}.directness: must be 1.0 (stated) or 0.6 (inferred)")
    rationale = _text(raw["rationale"], f"{where}.rationale", errors, MAX_RATIONALE_CHARS)
    evidence = _parse_evidence(raw["evidence"], f"{where}.evidence", errors)
    valid_from = _day(raw["valid_from"], f"{where}.valid_from", errors)
    curated_at = _day(raw["curated_at"], f"{where}.curated_at", errors)
    review_by = _day(raw["review_by"], f"{where}.review_by", errors)
    curated_by = raw["curated_by"]
    if curated_by not in CURATORS:
        errors.append(f"{where}.curated_by: must be a named human curator {sorted(CURATORS)}")
    proposal_id = _pattern(raw["proposal_id"], f"{where}.proposal_id", SLUG, errors)
    drafted_by = _pattern(raw["drafted_by"], f"{where}.drafted_by", DRAFTED_BY, errors)
    if source and target and source.casefold() == target.casefold():
        errors.append(f"{where}: an edge cannot connect an entity to itself")
    if valid_from and curated_at and valid_from > curated_at:
        errors.append(f"{where}.valid_from: cannot be after curated_at")
    if curated_at and review_by and review_by <= curated_at:
        errors.append(f"{where}.review_by: must be after curated_at")
    if curated_at and curated_at > today:
        errors.append(f"{where}.curated_at: cannot be in the future")
    if errors:
        raise CurationValidationError(errors)
    return CuratedEdge(
        source=source,  # type: ignore[arg-type]
        relation=relation,
        target=target,  # type: ignore[arg-type]
        weight=weight,  # type: ignore[arg-type]
        directness=float(directness),
        rationale=rationale,  # type: ignore[arg-type]
        evidence=evidence,  # type: ignore[arg-type]
        valid_from=valid_from,  # type: ignore[arg-type]
        curated_by=curated_by,
        curated_at=curated_at,  # type: ignore[arg-type]
        review_by=review_by,  # type: ignore[arg-type]
        proposal_id=proposal_id,  # type: ignore[arg-type]
        drafted_by=drafted_by,  # type: ignore[arg-type]
    )


def _parse_entity(raw: Any, where: str, errors: list[str]) -> CuratedEntity | None:
    before = len(errors)
    if not _mapping(raw, where, ENTITY_FIELDS, errors):
        return None
    entity_id = _pattern(raw["entity_id"], f"{where}.entity_id", CURATED_ENTITY_ID, errors)
    name = _text(raw["canonical_name"], f"{where}.canonical_name", errors)
    entity_type = raw["type"]
    if entity_type not in ENTITY_TYPES:
        errors.append(f"{where}.type: must be one of the frozen entity types")
    aliases = raw["aliases"]
    if not isinstance(aliases, list) or not aliases:
        errors.append(f"{where}.aliases: at least one alias is required")
        aliases = []
    for index, alias in enumerate(aliases):
        if _text(alias, f"{where}.aliases[{index}]", errors) is None:
            continue
        try:
            normalize_alias(alias)
        except CatalogInputError:
            errors.append(f"{where}.aliases[{index}]: normalizes to an empty alias")
    if len(errors) != before:
        return None
    return CuratedEntity(entity_id, name, entity_type, tuple(aliases))  # type: ignore[arg-type]


def parse_curation_file(payload: Any, today: date | None = None) -> CurationFile:
    """Validate the whole curated file offline before any database or graph work."""
    today = today or utc_today()
    errors: list[str] = []
    if not isinstance(payload, Mapping) or set(payload) != {"entities", "edges"}:
        raise CurationValidationError(["file: must contain exactly `entities` and `edges`"])
    raw_entities, raw_edges = payload["entities"], payload["edges"]
    if not isinstance(raw_entities, list) or not isinstance(raw_edges, list):
        raise CurationValidationError(["file: `entities` and `edges` must be lists"])
    entities: list[CuratedEntity] = []
    for index, raw in enumerate(raw_entities):
        entity = _parse_entity(raw, f"entities[{index}]", errors)
        if entity:
            entities.append(entity)
    if len({entity.entity_id for entity in entities}) != len(entities):
        errors.append("entities: entity_id values must be unique")
    alias_owner: dict[str, str] = {}
    for entity in entities:
        for alias in entity.aliases:
            norm = normalize_alias(alias)
            owner = alias_owner.setdefault(norm, entity.entity_id)
            if owner != entity.entity_id:
                errors.append(f"entities: alias {alias!r} is declared for {owner} and "
                              f"{entity.entity_id}")
    edges: list[CuratedEdge] = []
    for index, raw in enumerate(raw_edges):
        try:
            edges.append(parse_edge(raw, f"edges[{index}]", today))
        except CurationValidationError as error:
            errors.extend(error.errors)
    errors.extend(_duplicate_edge_errors(edges))
    if errors:
        raise CurationValidationError(errors)
    return CurationFile(tuple(entities), tuple(edges))


def _duplicate_edge_errors(edges: Iterable[CuratedEdge]) -> list[str]:
    errors: list[str] = []
    triples: dict[tuple[str, str, str], str] = {}
    proposals: set[str] = set()
    for edge in edges:
        prior = triples.setdefault(edge.surface_key, edge.proposal_id)
        if prior != edge.proposal_id:
            errors.append(
                f"edges: {edge.source} -{edge.relation}-> {edge.target} appears in both "
                f"{prior} and {edge.proposal_id}; revise one edge instead"
            )
        if edge.proposal_id in proposals:
            errors.append(f"edges: proposal_id {edge.proposal_id} appears more than once")
        proposals.add(edge.proposal_id)
    return errors


def read_curation_file(path: Path = DEFAULT_EDGES_PATH, today: date | None = None) -> CurationFile:
    return parse_curation_file(yaml.safe_load(path.read_text(encoding="utf-8")), today)


def _yaml_edge(edge: CuratedEdge) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for field in EDGE_FIELDS:
        value = getattr(edge, field)
        if field == "evidence":
            value = [
                {key: item for key, item in evidence.canonical().items()
                 if key != "locator" or item is not None}
                for evidence in edge.evidence
            ]
        rendered[field] = value
    return rendered


class _PlainDumper(yaml.SafeDumper):
    """No `&id001` anchors when edges share a date object; reviewers read literal values."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def render_curation_file(curation: CurationFile) -> str:
    """Deterministic rendering so each review produces a minimal, stable PR diff."""
    payload = {
        "entities": [
            {
                "entity_id": entity.entity_id,
                "canonical_name": entity.canonical_name,
                "type": entity.entity_type,
                "aliases": list(entity.aliases),
            }
            for entity in curation.entities
        ],
        "edges": [_yaml_edge(edge) for edge in curation.edges],
    }
    body = yaml.dump(
        payload, Dumper=_PlainDumper, sort_keys=False, allow_unicode=True, width=100
    )
    return EDGES_FILE_HEADER + body


# ---------------------------------------------------------------------------
# Review log: append-only JSONL, one decision per proposal.


def _parse_review_record(raw: Any, where: str) -> ReviewRecord:
    errors: list[str] = []
    if not _mapping(raw, where, REVIEW_FIELDS, errors):
        raise CurationValidationError(errors)
    if raw["schema"] != REVIEW_SCHEMA:
        errors.append(f"{where}.schema: must be {REVIEW_SCHEMA}")
    decision = raw["decision"]
    if decision not in {"accepted", "edited", "rejected"}:
        errors.append(f"{where}.decision: must be accepted, edited, or rejected")
    if raw["reviewer"] not in CURATORS:
        errors.append(f"{where}.reviewer: must be a named human curator")
    decided_at = _day(raw["decided_at"], f"{where}.decided_at", errors)
    _pattern(raw["batch_id"], f"{where}.batch_id", SLUG, errors)
    _pattern(raw["proposal_id"], f"{where}.proposal_id", SLUG, errors)
    _pattern(raw["drafted_by"], f"{where}.drafted_by", DRAFTED_BY, errors)
    edge, curated_sha = raw["edge"], raw["curated_sha"]
    if decision == "rejected":
        if edge is not None or curated_sha is not None:
            errors.append(f"{where}: a rejected proposal cannot carry an accepted edge")
        if not isinstance(raw["reason"], str) or not raw["reason"].strip():
            errors.append(f"{where}.reason: rejected proposals must record a reason")
    elif decision in {"accepted", "edited"}:
        if not isinstance(edge, Mapping) or not isinstance(curated_sha, str):
            errors.append(f"{where}: an accepting decision must record the edge and its SHA")
        elif content_sha(edge) != curated_sha:
            errors.append(f"{where}.curated_sha: does not hash the recorded edge")
        elif edge.get("proposal_id") != raw["proposal_id"]:
            errors.append(f"{where}.edge.proposal_id: does not match the review record")
    draft, draft_sha = raw["draft"], raw["draft_sha"]
    if draft is not None and (not isinstance(draft, Mapping) or content_sha(draft) != draft_sha):
        errors.append(f"{where}.draft_sha: does not hash the recorded draft")
    if errors:
        raise CurationValidationError(errors)
    return ReviewRecord(
        proposal_id=raw["proposal_id"],
        batch_id=raw["batch_id"],
        decision=decision,
        reviewer=raw["reviewer"],
        decided_at=decided_at,  # type: ignore[arg-type]
        drafted_by=raw["drafted_by"],
        usage_request_id=raw["usage_request_id"],
        draft=draft,
        draft_sha=draft_sha,
        proposed_edge=raw["proposed_edge"],
        edge=edge,
        curated_sha=curated_sha,
        reason=raw["reason"],
    )


def parse_review_log(lines: Iterable[str]) -> ReviewLog:
    records: list[ReviewRecord] = []
    errors: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        where = f"review_log:{number}"
        try:
            records.append(_parse_review_record(json.loads(line), where))
        except json.JSONDecodeError:
            errors.append(f"{where}: invalid JSON")
        except CurationValidationError as error:
            errors.extend(error.errors)
    seen: set[str] = set()
    for record in records:
        if record.proposal_id in seen:
            errors.append(f"review_log: proposal {record.proposal_id} was decided more than once")
        seen.add(record.proposal_id)
    if errors:
        raise CurationValidationError(errors)
    return ReviewLog(tuple(records))


def read_review_log(path: Path = DEFAULT_REVIEW_LOG_PATH) -> ReviewLog:
    if not path.exists():
        return ReviewLog(())
    return parse_review_log(path.read_text(encoding="utf-8").splitlines())


def unreviewed_edges(curation: CurationFile, log: ReviewLog) -> tuple[CuratedEdge, ...]:
    """Edges with no accepting review of their exact content (unreviewed or later changed)."""
    accepted = log.accepted_shas
    return tuple(edge for edge in curation.edges if edge.sha not in accepted)


def require_reviewed(curation: CurationFile, log: ReviewLog) -> None:
    missing = unreviewed_edges(curation, log)
    if missing:
        raise CurationValidationError(
            [
                f"edge {edge.proposal_id} (curated:{edge.sha}) has no accept/edit review "
                "record; it is unreviewed or was changed after review"
                for edge in missing
            ]
        )


def resolve_curated_sha(
    sha: str, curation: CurationFile, log: ReviewLog
) -> tuple[CuratedEdge | None, ReviewRecord | None]:
    """Map `curated:<sha>` provenance back to the reviewed source text."""
    sha = sha.removeprefix("curated:")
    edge = next((edge for edge in curation.edges if edge.sha == sha), None)
    return edge, log.find_sha(sha)


# ---------------------------------------------------------------------------
# Review: apply a proposal batch's human decisions.


@dataclass(frozen=True)
class ReviewOutcome:
    decided: tuple[ReviewRecord, ...]
    already_decided: tuple[str, ...]
    pending: tuple[str, ...]
    curation: CurationFile


def draft_directness(label: Any) -> float | None:
    return DIRECTNESS_VALUES.get(label) if isinstance(label, str) else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


def _is_edited(draft: Mapping[str, Any] | None, edge: CuratedEdge) -> bool:
    if draft is None:
        return False
    if any(draft.get(field) != getattr(edge, field) for field in DRAFT_COMPARED_FIELDS):
        return True
    return draft_directness(draft.get("directness")) != edge.directness


def review_batch(
    batch: Any,
    curation: CurationFile,
    log: ReviewLog,
    reviewer: str,
    today: date | None = None,
) -> ReviewOutcome:
    """Validate a whole proposal batch and compute its review records without writing."""
    today = today or utc_today()
    errors: list[str] = []
    if reviewer not in CURATORS:
        raise CurationValidationError([f"reviewer: must be one of {sorted(CURATORS)}"])
    if not _mapping(batch, "batch", BATCH_FIELDS, errors):
        raise CurationValidationError(errors)
    batch_id = _pattern(batch["batch_id"], "batch.batch_id", SLUG, errors)
    drafted_by = _pattern(batch["drafted_by"], "batch.drafted_by", DRAFTED_BY, errors)
    usage_request_id = batch["usage_request_id"]
    if drafted_by and drafted_by.startswith("llm:"):
        try:
            usage_request_id = str(UUID(str(usage_request_id)))
        except ValueError:
            errors.append("batch.usage_request_id: LLM drafts must link their metered request")
    elif usage_request_id is not None:
        errors.append("batch.usage_request_id: human-authored batches have no model request")
    proposals = batch["proposals"]
    if not isinstance(proposals, list):
        raise CurationValidationError(errors + ["batch.proposals: must be a list"])
    if errors:
        raise CurationValidationError(errors)

    decided: list[ReviewRecord] = []
    already: list[str] = []
    pending: list[str] = []
    accepted_edges: list[CuratedEdge] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(proposals):
        where = f"batch.proposals[{index}]"
        if not _mapping(raw, where, PROPOSAL_FIELDS, errors):
            continue
        proposal_id = _pattern(raw["proposal_id"], f"{where}.proposal_id", SLUG, errors)
        if proposal_id is None:
            continue
        if proposal_id in seen_ids:
            errors.append(f"{where}.proposal_id: duplicate proposal {proposal_id}")
            continue
        seen_ids.add(proposal_id)
        draft, draft_sha = raw["draft"], raw["draft_sha"]
        if drafted_by.startswith("llm:"):
            if not isinstance(draft, Mapping) or set(draft) - {*DRAFT_FIELDS, "excerpt_verified"}:
                errors.append(f"{where}.draft: LLM proposals must keep their draft snapshot")
                continue
            if content_sha(draft) != draft_sha:
                errors.append(f"{where}.draft: draft snapshot changed after drafting")
                continue
        elif draft is not None or draft_sha is not None:
            errors.append(f"{where}.draft: human-authored proposals have no draft snapshot")
            continue
        decision = raw["decision"]
        if proposal_id in log.decided_proposal_ids:
            already.append(proposal_id)
            continue
        if decision == "pending":
            pending.append(proposal_id)
            continue
        if decision == "reject":
            reason = raw["reason"]
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{where}.reason: explain every rejection; it is training data")
                continue
            decided.append(
                ReviewRecord(
                    proposal_id, batch_id, "rejected", reviewer, today, drafted_by,
                    usage_request_id, _jsonable(draft), draft_sha, _jsonable(raw["edge"]),
                    None, None, reason.strip(),
                )
            )
            continue
        if decision != "accept":
            errors.append(f"{where}.decision: must be pending, accept, or reject")
            continue
        raw_edge = raw["edge"]
        if not isinstance(raw_edge, Mapping) or set(raw_edge) != set(PROPOSAL_EDGE_FIELDS):
            errors.append(f"{where}.edge: must contain exactly {list(PROPOSAL_EDGE_FIELDS)}")
            continue
        try:
            edge = parse_edge(
                dict(raw_edge)
                | {
                    "curated_by": reviewer,
                    "curated_at": today,
                    "proposal_id": proposal_id,
                    "drafted_by": drafted_by,
                },
                f"{where}.edge",
                today,
            )
        except CurationValidationError as error:
            errors.extend(error.errors)
            continue
        accepted_edges.append(edge)
        decided.append(
            ReviewRecord(
                proposal_id, batch_id, "edited" if _is_edited(draft, edge) else "accepted",
                reviewer, today, drafted_by, usage_request_id, _jsonable(draft), draft_sha,
                None, edge.canonical(), edge.sha, None,
            )
        )

    merged = _merge_accepted(curation, accepted_edges, errors)
    if errors:
        raise CurationValidationError(errors)
    return ReviewOutcome(tuple(decided), tuple(already), tuple(pending), merged)


def _merge_accepted(
    curation: CurationFile, accepted: Sequence[CuratedEdge], errors: list[str]
) -> CurationFile:
    """Add accepted edges; a later-valid revision replaces its predecessor in the file.

    The superseded text stays resolvable through its review record, and the loader
    closes the predecessor's validity in the graph. A crash between writing the file
    and the log leaves an identical edge behind, which is kept rather than duplicated.
    """
    edges = list(curation.edges)
    for edge in accepted:
        if any(existing.sha == edge.sha for existing in edges):
            continue
        index = next(
            (i for i, existing in enumerate(edges) if existing.surface_key == edge.surface_key),
            None,
        )
        if index is None:
            edges.append(edge)
        elif edges[index].valid_from < edge.valid_from:
            edges[index] = edge
        else:
            errors.append(
                f"{edge.proposal_id}: revises {edges[index].proposal_id} but its valid_from "
                f"must be after {edges[index].valid_from.isoformat()}"
            )
    merged = CurationFile(curation.entities, tuple(edges))
    errors.extend(_duplicate_edge_errors(merged.edges))
    return merged


def write_review_outcome(
    outcome: ReviewOutcome,
    edges_path: Path = DEFAULT_EDGES_PATH,
    log_path: Path = DEFAULT_REVIEW_LOG_PATH,
) -> None:
    """Write edges first, then the log: an interrupted run can never load unreviewed text."""
    if not outcome.decided:
        return
    edges_path.write_text(render_curation_file(outcome.curation), encoding="utf-8")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        for record in outcome.decided:
            log_file.write(json.dumps(record.as_json(), sort_keys=True, ensure_ascii=False))
            log_file.write("\n")


# ---------------------------------------------------------------------------
# Endpoint resolution and load planning.


@dataclass(frozen=True)
class ResolvedEndpoint:
    entity_id: str
    canonical_name: str
    entity_type: str
    resolution_quality: float


class EndpointNotResolved(LookupError):
    pass


class EndpointResolver(Protocol):
    def resolve(self, surface: str) -> ResolvedEndpoint: ...


class PostgresCurationCatalog:
    """Strict rungs 1–2 for curation: an identifier or exactly one alias, never a guess.

    Unlike the article resolver, this never breaks ties by seed_count and never writes
    `resolution_misses`: an unresolved curated endpoint is a validation error to fix.
    """

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def upsert_curated_entities(self, entities: Iterable[CuratedEntity]) -> None:
        for entity in entities:
            self._connection.execute(
                """INSERT INTO entities (entity_id, canonical_name, type)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (entity_id) DO UPDATE SET
                     canonical_name = EXCLUDED.canonical_name, type = EXCLUDED.type""",
                (entity.entity_id, entity.canonical_name, entity.entity_type),
            )
            for alias in entity.aliases:
                self._connection.execute(
                    """INSERT INTO entity_aliases
                       (alias_norm, entity_id, source, confidence, is_identifier)
                       VALUES (%s, %s, 'curated', 1.0, false)
                       ON CONFLICT (alias_norm, entity_id) DO NOTHING""",
                    (normalize_alias(alias), entity.entity_id),
                )

    def resolve(self, surface: str) -> ResolvedEndpoint:
        if ENTITY_ID.fullmatch(surface):
            row = self._connection.execute(
                "SELECT entity_id, canonical_name, type FROM entities WHERE entity_id = %s",
                (surface,),
            ).fetchone()
            if row is None:
                raise EndpointNotResolved(f"{surface!r} is not a known entity_id")
            return ResolvedEndpoint(*row, IDENTIFIER_RESOLUTION_QUALITY)
        try:
            key, rung = alias_key(surface)
        except CatalogInputError as error:
            raise EndpointNotResolved(f"{surface!r} has no resolvable alias") from error
        rows = self._connection.execute(
            """SELECT entities.entity_id, entities.canonical_name, entities.type
               FROM entity_aliases JOIN entities USING (entity_id)
               WHERE entity_aliases.alias_norm = %s ORDER BY entities.entity_id""",
            (key,),
        ).fetchall()
        if not rows:
            raise EndpointNotResolved(
                f"{surface!r} did not resolve; use a ticker, CIK, entity_id, or declared alias"
            )
        if len(rows) > 1:
            candidates = ", ".join(row[0] for row in rows)
            raise EndpointNotResolved(f"{surface!r} is ambiguous ({candidates}); use an ID")
        quality = IDENTIFIER_RESOLUTION_QUALITY if rung == 1 else ALIAS_RESOLUTION_QUALITY
        return ResolvedEndpoint(*rows[0], quality)


@dataclass(frozen=True)
class PlannedCuratedEdge:
    edge: CuratedEdge
    source: ResolvedEndpoint
    target: ResolvedEndpoint
    revision: TemporalEdgeRevision


def _midnight_utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def curated_confidence(edge: CuratedEdge, resolution_quality: float) -> float:
    """§9.1 confidence from the reviewed evidence; publisher groups, not URLs, count."""
    return confidence(
        source_quality_max=max(item.tier_value for item in edge.evidence),
        publisher_groups=len({item.publisher_group for item in edge.evidence}),
        directness=edge.directness,
        resolution_quality=resolution_quality,
    )


def plan_curated_load(
    curation: CurationFile, log: ReviewLog, resolver: EndpointResolver
) -> tuple[PlannedCuratedEdge, ...]:
    """Resolve and score every reviewed edge; any problem aborts before graph writes.

    Every revision field is a pure function of the reviewed entry and its resolved
    endpoints, so reloading replays the same graph request instead of conflicting.
    """
    require_reviewed(curation, log)
    errors: list[str] = []
    planned: list[PlannedCuratedEdge] = []
    triples: dict[tuple[str, str, str], str] = {}
    for edge in curation.edges:
        try:
            source = resolver.resolve(edge.source)
            target = resolver.resolve(edge.target)
        except EndpointNotResolved as error:
            errors.append(f"edge {edge.proposal_id}: {error}")
            continue
        if source.entity_id == target.entity_id:
            errors.append(f"edge {edge.proposal_id}: both endpoints resolve to {source.entity_id}")
            continue
        triple = (source.entity_id, edge.relation, target.entity_id)
        prior = triples.setdefault(triple, edge.proposal_id)
        if prior != edge.proposal_id:
            errors.append(f"edge {edge.proposal_id}: resolves to the same edge as {prior}")
            continue
        quality = min(source.resolution_quality, target.resolution_quality)
        revision = TemporalEdgeRevision(
            request_id=curation_request_id(edge.sha),
            from_entity_id=source.entity_id,
            to_entity_id=target.entity_id,
            relation_type=edge.relation,
            weight=edge.weight,
            confidence=curated_confidence(edge, quality),
            valid_from=_midnight_utc(edge.valid_from),
            observed_at=_midnight_utc(edge.curated_at),
            source_ids=(edge.source_id,),
            evidence_count=len(edge.evidence),
            directness=edge.directness,
            origin=CURATED_ORIGIN,
        )
        planned.append(PlannedCuratedEdge(edge, source, target, revision))
    if errors:
        raise CurationValidationError(errors)
    return tuple(planned)


@dataclass(frozen=True)
class CuratedLoadResult:
    proposal_id: str
    source_id: str
    status: str  # created | replayed | failed
    revision_id: str | None
    confidence: float
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "source_id": self.source_id,
            "status": self.status,
            "revision_id": self.revision_id,
            "confidence": round(self.confidence, 4),
            "error": self.error,
        }


def _explain_graph_error(error: GraphWriteError) -> str:
    message = str(error)
    if "stale temporal revision" in message:
        return (
            f"{message}: a changed curated edge needs a valid_from later than the loaded "
            "revision's valid_from"
        )
    if "conflicts with an existing graph write" in message:
        return f"{message}: an endpoint now resolves differently than when this edge loaded"
    return message


def apply_curated_load(
    plan: Sequence[PlannedCuratedEdge], graph: GraphStore
) -> tuple[CuratedLoadResult, ...]:
    """Upsert endpoint nodes, then write each edge; reruns replay, never duplicate."""
    endpoints = {
        endpoint.entity_id: endpoint
        for item in plan
        for endpoint in (item.source, item.target)
    }
    for endpoint in endpoints.values():
        graph.upsert_entity(endpoint.entity_id, endpoint.canonical_name, endpoint.entity_type)
    results: list[CuratedLoadResult] = []
    for item in plan:
        try:
            written = graph.create_curated_edge_revision(item.revision)
        except GraphWriteError as error:
            results.append(
                CuratedLoadResult(
                    item.edge.proposal_id, item.edge.source_id, "failed", None,
                    item.revision.confidence, _explain_graph_error(error),
                )
            )
            continue
        results.append(
            CuratedLoadResult(
                item.edge.proposal_id,
                item.edge.source_id,
                "replayed" if written.replayed else "created",
                written.revision_id,
                item.revision.confidence,
            )
        )
    return tuple(results)
