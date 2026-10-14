"""T22 retention, routing, and the recall audit for the three-tier gate (§7.2–7.3).

- Tier 2 is quarantined for 30 days and counted in demand_log (written with the
  article by the T20 stage, so the two can never disagree).
- Tier-3 metadata lives 48 hours from ingestion; audited articles are kept.
- Sources with tier_value >= 0.9 are always fetched (override mode).
- 5% of tier 3, chosen reproducibly from the URL hash, is fetched and audited.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from app.worker import celery_app

logger = logging.getLogger(__name__)

QUARANTINE_TTL = timedelta(days=30)
TIER3_TTL = timedelta(hours=48)
HIGH_TIER_VALUE = 0.9
AUDIT_RATE_PERCENT = 5


def audit_sampled(url_hash: str) -> bool:
    """Reproducible 5% sample: the same URL is always in or out of the audit."""
    return int(url_hash[:8], 16) % 100 < AUDIT_RATE_PERCENT


def body_mode(tier: int, tier_value: float, sampled: bool) -> str | None:
    """Which T21 treatment a newly tiered article gets, or None for metadata only."""
    if tier == 1:
        return "default"
    if tier_value >= HIGH_TIER_VALUE:
        return "override"  # primary sources are always read (§7.2)
    if tier == 2:
        return "default"  # title+snippet embedding, no fetch
    return "audit" if sampled else None


def week_start(moment: datetime) -> datetime:
    day = moment.astimezone(UTC).date()
    return datetime.combine(day - timedelta(days=day.weekday()), datetime.min.time(), UTC)


def quarantine_article(connection: psycopg.Connection[Any], article_id: Any,
                       entity_ids: tuple[str, ...], now: datetime) -> None:
    """Quarantine a tier-2 article and count demand for each catalog entity it names.

    Called inside the T20 insert transaction so an article is never tier 2 without
    its quarantine row and demand count.
    """
    connection.execute(
        """INSERT INTO quarantine (article_id, tagged_entity_ids, expires_at)
           VALUES (%s, %s, %s) ON CONFLICT (article_id) DO NOTHING""",
        (article_id, list(entity_ids), now + QUARANTINE_TTL),
    )
    for entity_id in entity_ids:
        connection.execute(
            """INSERT INTO demand_log (entity_id, quarantined_article_count, week)
               VALUES (%s, 1, %s)
               ON CONFLICT (entity_id, week) DO UPDATE
               SET quarantined_article_count = demand_log.quarantined_article_count + 1""",
            (entity_id, week_start(now).date()),
        )


@dataclass(frozen=True)
class PurgeReport:
    tier2: int
    tier3: int


def purge_expired(connection: psycopg.Connection[Any], now: datetime) -> PurgeReport:
    """Drop expired quarantine and tier-3 metadata; keep audited and clustered articles."""
    with connection.transaction():
        rows = connection.execute(
            """DELETE FROM articles a
               WHERE ((a.tier = 3 AND a.ingested_at < %(tier3_cutoff)s)
                      OR (a.tier = 2 AND a.ingested_at < %(tier2_cutoff)s))
                 AND NOT EXISTS (SELECT 1 FROM recall_audit r WHERE r.article_id = a.id)
                 AND NOT EXISTS (SELECT 1 FROM event_articles e WHERE e.article_id = a.id)
                 AND NOT EXISTS (SELECT 1 FROM event_claims c WHERE c.article_id = a.id)
               RETURNING a.tier""",
            {"tier3_cutoff": now - TIER3_TTL, "tier2_cutoff": now - QUARANTINE_TTL},
        ).fetchall()
    tiers = [row[0] for row in rows]
    report = PurgeReport(tiers.count(2), tiers.count(3))
    logger.info("expired articles purged", extra=report.__dict__)
    return report


def ingest_report(connection: psycopg.Connection[Any], since: datetime) -> dict[str, Any]:
    """Daily tier mix and the §17 tier-3 recall metric, for the weekly review."""
    days = connection.execute(
        """SELECT ingested_at::date AS day, tier, count(*),
                  count(*) FILTER (WHERE fetched_at IS NOT NULL),
                  count(*) FILTER (WHERE embedding IS NOT NULL)
           FROM articles WHERE ingested_at >= %s
           GROUP BY 1, 2 ORDER BY 1, 2""",
        (since,),
    ).fetchall()
    audited, with_live = connection.execute(
        """SELECT count(*), count(*) FILTER (WHERE EXISTS (
                      SELECT 1 FROM entities e
                      WHERE e.entity_id = ANY(r.entities_found_in_body) AND e.status = 'live'))
           FROM recall_audit r WHERE r.ts >= %s""",
        (since,),
    ).fetchone()
    return {
        "days": [{"day": day.isoformat(), "tier": tier, "articles": count, "fetched": fetched,
                  "embedded": embedded} for day, tier, count, fetched, embedded in days],
        "tier3_audit": {
            "audited": audited,
            "with_live_entities": with_live,
            # §17: < 10% ship, > 20% the metadata filter is too tight.
            "share": round(with_live / audited, 4) if audited else None,
        },
    }


@celery_app.task(name="app.quarantine.purge_expired_articles")
def purge_expired_articles() -> dict[str, int]:
    with psycopg.connect(os.environ["TEVARTA_POSTGRES_URL"]) as connection:
        return purge_expired(connection, datetime.now(UTC)).__dict__
