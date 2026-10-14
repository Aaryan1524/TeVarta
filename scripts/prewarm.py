#!/usr/bin/env python3
"""Pre-warm likely seeds (T24): load reviewed aliases, expand each entity, report gaps."""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from app.aliases import apply_aliases, read_alias_file
from app.expansion import (
    EntityExpander,
    enqueue_candidate,
    industry_source_from_env,
    search_service_from_env,
)
from app.graph_store import GraphStore
from app.prewarm import prewarm, read_prewarm_list
from neo4j import GraphDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--neo4j-url", required=True)
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-database", default="neo4j")
    parser.add_argument("--no-search", action="store_true",
                        help="skip the 30-day targeted search (saves search budget)")
    args = parser.parse_args()
    password = os.environ.get("TEVARTA_NEO4J_PASSWORD")
    if not password:
        print("set TEVARTA_NEO4J_PASSWORD", file=sys.stderr)
        return 2
    references = read_prewarm_list()
    with psycopg.connect(args.database_url) as connection, GraphDatabase.driver(
        args.neo4j_url, auth=(args.neo4j_user, password)
    ) as driver:
        aliases = apply_aliases(connection, read_alias_file())
        graph = GraphStore(driver, database=args.neo4j_database)
        graph.initialize()
        search = None if args.no_search else search_service_from_env()
        expander = EntityExpander(connection, graph, industry_source_from_env(),
                                  search=search, sink=enqueue_candidate if search else None)
        report = prewarm(connection, expander, references)
    gaps = [item for item in report if item.gap]
    print(json.dumps({
        "aliases": aliases.as_dict(),
        "entities": len(report),
        "live": sum(item.live for item in report),
        "with_dependency_edges": sum(item.dependency_edges > 0 for item in report),
        "gaps": [{"entity": item.reference, "name": item.name, "view": item.view,
                  "dependency_edges": item.dependency_edges} for item in gaps],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
