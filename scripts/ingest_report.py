#!/usr/bin/env python3
"""Daily tier mix and the §17 tier-3 recall metric for the weekly review (T22)."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

import psycopg
from app.quarantine import ingest_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    since = datetime.now(UTC) - timedelta(days=args.days)
    with psycopg.connect(args.database_url) as connection:
        print(json.dumps(ingest_report(connection, since), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
