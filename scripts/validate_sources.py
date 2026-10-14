#!/usr/bin/env python3
"""T19 source validation: fetch each candidate feed once, parse it, and check robots.txt.

Read-only and polite (one request per feed plus robots.txt). Activation stays a
founder decision: this reports evidence, it never sets `active: true`.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

from app.bodies import RobotsPolicy, http_get
from app.discovery import FeedPoller, declared_user_agent, load_sources


def main() -> int:
    agent = declared_user_agent()
    robots, poller = RobotsPolicy(http_get, agent), FeedPoller(user_agent=agent)
    report = []
    for source in load_sources():
        row = {"id": source.id, "tier_value": source.tier_value,
               "publisher_group": source.publisher_group, "active": source.active}
        if not source.feed_url:
            report.append({**row, "feed": "none (licensed source)"})
            continue
        try:
            items = poller.poll(replace(source, active=True))
            row.update(feed="ok", items=len(items),
                       sample=items[0].title[:80] if items else None)
            sample_url = items[0].url if items else None
        except Exception as error:
            row.update(feed=f"error: {type(error).__name__}: {str(error)[:80]}")
            sample_url = None
        try:
            row["robots_feed"] = robots.allowed(source.feed_url)
            row["robots_article"] = robots.allowed(sample_url) if sample_url else None
        except Exception as error:
            row["robots_feed"] = f"error: {type(error).__name__}"
        report.append(row)
        time.sleep(0.3)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
