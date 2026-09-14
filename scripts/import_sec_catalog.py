#!/usr/bin/env python3
"""Import a downloaded SEC company_tickers.json into a Te Varta database."""

from __future__ import annotations

import argparse
import json

import psycopg
from app.sec_catalog import import_sec_catalog, parse_company_tickers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", help="path to downloaded SEC company_tickers.json")
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    with open(args.catalog, encoding="utf-8") as catalog_file, psycopg.connect(args.database_url) as connection:
        report = import_sec_catalog(connection, parse_company_tickers(json.load(catalog_file)))
    print(json.dumps(report.as_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
