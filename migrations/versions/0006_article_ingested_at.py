"""Ingestion time for article retention (T22).

Revision ID: 0006_article_ingested_at
Revises: 0005_expansion_lifecycle

Tier-3 metadata lives 48 hours from ingestion. published_at cannot serve: an old
article discovered today would be purged on the next run. seen_urls then stops a
purged article being re-ingested (and re-fetched) while its feed still lists it.
"""

from alembic import op

revision = "0006_article_ingested_at"
down_revision = "0005_expansion_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE articles ADD COLUMN ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();
        CREATE INDEX ix_articles_tier_ingested ON articles (tier, ingested_at);
        -- A purged article must not be re-ingested while its feed still lists it: dedupe
        -- consults this tombstone of the URL hash (a hash is not metadata).
        CREATE TABLE seen_urls (
            url_hash TEXT PRIMARY KEY,
            first_seen TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_seen_urls_first_seen ON seen_urls (first_seen);
        INSERT INTO seen_urls (url_hash) SELECT url_hash FROM articles;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE seen_urls;
        DROP INDEX ix_articles_tier_ingested;
        ALTER TABLE articles DROP COLUMN ingested_at;
    """)
