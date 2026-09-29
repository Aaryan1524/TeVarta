#!/usr/bin/env python3
"""Draft, review, validate, load, and trace reviewed curated edges (T14)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg
import yaml
from app.curation import (
    DEFAULT_EDGES_PATH,
    DEFAULT_REVIEW_LOG_PATH,
    CurationValidationError,
    PostgresCurationCatalog,
    apply_curated_load,
    parse_evidence,
    plan_curated_load,
    read_curation_file,
    read_review_log,
    require_reviewed,
    resolve_curated_sha,
    review_batch,
    write_review_outcome,
)
from app.curation_draft import (
    DraftConfigurationError,
    DraftModelError,
    DraftRequest,
    OpenAIDraftModel,
    PostgresUsageMeter,
    TokenPrices,
    draft_batch,
)
from app.graph_store import GraphStore
from neo4j import GraphDatabase


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _database_url(args: argparse.Namespace) -> str:
    url = args.database_url or os.environ.get("TEVARTA_POSTGRES_URL")
    if not url:
        raise DraftConfigurationError("pass --database-url or set TEVARTA_POSTGRES_URL")
    return url


def _prices() -> TokenPrices:
    try:
        return TokenPrices(
            Decimal(os.environ["TEVARTA_CURATION_DRAFT_USD_PER_MTOK_INPUT"]),
            Decimal(os.environ["TEVARTA_CURATION_DRAFT_USD_PER_MTOK_OUTPUT"]),
        )
    except (KeyError, InvalidOperation) as error:
        raise DraftConfigurationError(
            "set TEVARTA_CURATION_DRAFT_USD_PER_MTOK_INPUT and _OUTPUT to the model's "
            "current prices so every draft is costed"
        ) from error


def command_validate(args: argparse.Namespace) -> int:
    curation = read_curation_file(args.edges)
    log = read_review_log(args.review_log)
    require_reviewed(curation, log)
    report: dict[str, object] = {
        "entities": len(curation.entities),
        "edges": len(curation.edges),
        "review_records": len(log.records),
    }
    if args.database_url:
        with psycopg.connect(args.database_url) as connection:
            catalog = PostgresCurationCatalog(connection)
            catalog.upsert_curated_entities(curation.entities)
            plan = plan_curated_load(curation, log, catalog)
            connection.rollback()  # validation never changes the catalog
        report["resolved"] = [
            {
                "proposal_id": item.edge.proposal_id,
                "from": item.source.entity_id,
                "relation": item.edge.relation,
                "to": item.target.entity_id,
                "confidence": round(item.revision.confidence, 4),
            }
            for item in plan
        ]
    _print(report)
    return 0


def command_draft(args: argparse.Namespace) -> int:
    if args.out.exists():
        raise DraftConfigurationError(f"{args.out} already exists; drafts are never overwritten")
    request = DraftRequest(
        batch_id=args.batch_id,
        subject=args.subject,
        text=args.text_file.read_text(encoding="utf-8"),
        evidence=parse_evidence(
            [
                {
                    "url": args.evidence_url,
                    "source_type": args.source_type,
                    "publisher_group": args.publisher_group,
                    "locator": args.locator,
                }
            ]
        )[0],
    )
    meter = PostgresUsageMeter(_database_url(args), _prices())
    batch = draft_batch(request, OpenAIDraftModel.from_environ(), meter)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        yaml.safe_dump(batch, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
    )
    _print({"batch": str(args.out), "proposals": len(batch["proposals"]),
            "usage_request_id": batch["usage_request_id"]})
    return 0


def command_review(args: argparse.Namespace) -> int:
    batch = yaml.safe_load(args.proposals.read_text(encoding="utf-8"))
    outcome = review_batch(
        batch, read_curation_file(args.edges), read_review_log(args.review_log), args.reviewer
    )
    write_review_outcome(outcome, args.edges, args.review_log)
    _print(
        {
            "decided": {record.proposal_id: record.decision for record in outcome.decided},
            "already_decided": list(outcome.already_decided),
            "pending": list(outcome.pending),
        }
    )
    return 0


def command_load(args: argparse.Namespace) -> int:
    curation = read_curation_file(args.edges)
    log = read_review_log(args.review_log)
    with psycopg.connect(args.database_url) as connection:
        catalog = PostgresCurationCatalog(connection)
        catalog.upsert_curated_entities(curation.entities)
        plan = plan_curated_load(curation, log, catalog)  # raises -> rolls back
        connection.commit()
    password = os.environ.get("TEVARTA_NEO4J_PASSWORD")
    if not password:
        raise DraftConfigurationError("set TEVARTA_NEO4J_PASSWORD to load the World Graph")
    with GraphDatabase.driver(args.neo4j_url, auth=(args.neo4j_user, password)) as driver:
        store = GraphStore(driver, database=args.neo4j_database)
        store.initialize()
        results = apply_curated_load(plan, store)
    _print([result.as_dict() for result in results])
    return 1 if any(result.status == "failed" for result in results) else 0


def command_show(args: argparse.Namespace) -> int:
    edge, record = resolve_curated_sha(
        args.sha, read_curation_file(args.edges), read_review_log(args.review_log)
    )
    if edge is None and record is None:
        print(f"{args.sha} does not resolve to a reviewed curated edge", file=sys.stderr)
        return 1
    _print(
        {
            "current_edge": edge.canonical() if edge else None,
            "review_record": record.as_json() if record else None,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", type=Path, default=DEFAULT_EDGES_PATH)
    parser.add_argument("--review-log", type=Path, default=DEFAULT_REVIEW_LOG_PATH)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate schema and review gate")
    validate.add_argument("--database-url", help="also resolve endpoints and compute confidence")
    validate.set_defaults(handler=command_validate)

    draft = commands.add_parser("draft", help="metered LLM draft of candidate edges")
    draft.add_argument("--batch-id", required=True)
    draft.add_argument("--subject", required=True, help="subject ticker, e.g. AAPL")
    draft.add_argument("--text-file", type=Path, required=True)
    draft.add_argument("--evidence-url", required=True)
    draft.add_argument("--source-type", required=True)
    draft.add_argument("--publisher-group", required=True)
    draft.add_argument("--locator")
    draft.add_argument("--out", type=Path, required=True)
    draft.add_argument("--database-url")
    draft.set_defaults(handler=command_draft)

    review = commands.add_parser("review", help="apply accept/reject decisions from a batch")
    review.add_argument("proposals", type=Path)
    review.add_argument("--reviewer", required=True)
    review.set_defaults(handler=command_review)

    load = commands.add_parser("load", help="idempotently load reviewed edges into Neo4j")
    load.add_argument("--database-url", required=True)
    load.add_argument("--neo4j-url", required=True)
    load.add_argument("--neo4j-user", default="neo4j")
    load.add_argument("--neo4j-database", default="neo4j")
    load.set_defaults(handler=command_load)

    show = commands.add_parser("show", help="resolve curated:<sha> to its reviewed source")
    show.add_argument("sha")
    show.set_defaults(handler=command_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except CurationValidationError as error:
        for message in error.errors:
            print(f"error: {message}", file=sys.stderr)
        return 2
    except (DraftConfigurationError, DraftModelError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
