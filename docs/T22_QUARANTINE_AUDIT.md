# T22 — Quarantine, high-tier override, and the 5% recall audit

**Owner:** Aaryan
**MVP:** §3, §7.2 recall guard (change I), §7.3, §17 tier-3 audit criterion, §19

## Routing after tiering

`app.quarantine.body_mode` decides each new article's body treatment, and
`process_candidate` queues `process_article(article_id, mode)`:

| Article | Mode | What happens (T21 body stage) |
|---|---|---|
| tier 1 | `default` | body fetched, body embedding |
| `tier_value ≥ 0.9`, tier 2/3 | `override` | body always fetched; a live **company, security, facility or product named at least 3 times** in the body promotes it to tier 1 (quarantine row removed, body embedding) |
| tier 2 | `default` | title + snippet embedding, no fetch |
| tier 3, in the 5% sample | `audit` | body fetched and tagged; `recall_audit` stores the original tier and every entity found; never promoted or embedded |
| other tier 3 | — | metadata only |

The sample is reproducible: `int(url_hash[:8], 16) % 100 < 5`, so the same URL is
always in or out of it.

**Why the override promotes.** §7.2 always fetches primary sources because they
trigger the §8.1 fast path. An 8-K whose title names only catalog entities would
otherwise stay quarantined even when its body names a live company, which defeats
the purpose. The audit never promotes, so §17's tier-3 measurement stays clean. The
real-data run narrowed the rule. Countries appear in nearly every filing's risk
factors, and Bank of England minutes name banks once each in attendee lists, so only
a specific live subject that the body keeps returning to promotes. Broad entities
still reach tier 1 from headlines (§7.3).

## Quarantine and demand

The T20 stage writes the quarantine row (`expires_at` = ingestion + 30 days) and
increments `demand_log` per tagged catalog entity per UTC week, Monday-based, in the
same transaction as the tier-2 article. A tier-2 article therefore never lacks
either.

## Retention

Migration 0006 adds `articles.ingested_at`. The hourly
`purge_expired_articles` task deletes:

- tier 3 older than 48 hours, and
- tier 2 older than 30 days (its quarantine row cascades),

measured from ingestion. It keeps any article that is audited or referenced by an
event or a claim. Measuring from `published_at` would purge an old article as soon
as it was discovered. Purged articles leave a `seen_urls` tombstone (URL hash only,
kept 30 days), so a feed that still lists the item for days is not re-ingested and
its body is not fetched again every 48 hours.

## Weekly review

`scripts/ingest_report.py --database-url … --days 7` prints the daily tier mix
(articles, fetched, embedded) and the §17 metric: the share of audited tier-3
articles whose body names a live entity (ship < 10%, rethink > 20%).

## Verification

- `test_quarantine.py` 9 passed: the sample is reproducible and 4.5–5.5% over
  20,000 URLs; every row of the routing table; Monday weeks.
- `test_quarantine_database.py` 8 passed, covering:
  - quarantine expiry and demand counts per entity and week, exact in any order;
  - tier 1 and 3 never quarantined;
  - controlled-clock retention at 47/49 hours and 29/31 days, with audited
    articles kept;
  - override promotion when the body names a live entity, and no promotion
    without one;
  - the audit recording findings without promoting;
  - routing of primary sources and the sample;
  - the 0006 migration roundtrip.
