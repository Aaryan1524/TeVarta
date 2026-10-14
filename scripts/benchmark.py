#!/usr/bin/env python3
"""T27 clustering benchmark: collect candidates, check agreement, validate, score."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from app.benchmark import (
    agreement,
    collect_candidates,
    labels_from_rows,
    read_jsonl,
    score,
    validate_dataset,
    write_jsonl,
)

DATASET = Path(__file__).resolve().parents[1] / "benchmarks" / "clustering" / "v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="export tier-1/2 candidates with hints")
    collect.add_argument("--database-url", required=True)
    collect.add_argument("--days", type=int, default=14)
    collect.add_argument("--out", type=Path, default=DATASET / "candidates.jsonl")
    agree = commands.add_parser("agreement", help="compare two annotators' labels")
    agree.add_argument("first", type=Path)
    agree.add_argument("second", type=Path)
    validate = commands.add_parser("validate", help="is the benchmark ready for Week 3?")
    validate.add_argument("--dataset", type=Path, default=DATASET)
    scored = commands.add_parser("score", help="purity and fragmentation of a clustering")
    scored.add_argument("predicted", type=Path, help="jsonl of {id, event}")
    scored.add_argument("--dataset", type=Path, default=DATASET)
    args = parser.parse_args()

    if args.command == "collect":
        with psycopg.connect(args.database_url) as connection:
            rows = collect_candidates(connection, datetime.now(UTC) - timedelta(days=args.days))
        write_jsonl(args.out, rows)
        print(json.dumps({"candidates": len(rows), "out": str(args.out)}))
        return 0
    if args.command == "agreement":
        result = agreement(labels_from_rows(read_jsonl(args.first)),
                           labels_from_rows(read_jsonl(args.second)))
        print(json.dumps(result.as_dict(), indent=2))
        return 0
    dataset = args.dataset
    if args.command == "validate":
        report = validate_dataset(
            read_jsonl(dataset / "items.jsonl"),
            labels_from_rows(read_jsonl(dataset / "labels" / "resolved.jsonl")),
            labels_from_rows(read_jsonl(dataset / "labels" / "aaryan.jsonl")),
            labels_from_rows(read_jsonl(dataset / "labels" / "aaditya.jsonl")),
        )
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 1
    gold = labels_from_rows(read_jsonl(dataset / "labels" / "resolved.jsonl"))
    print(json.dumps(score(labels_from_rows(read_jsonl(args.predicted)), gold), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
