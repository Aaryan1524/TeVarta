"""Ingestion time for article retention (T22).

Revision ID: 0006_article_ingested_at
Revises: 0005_expansion_lifecycle

Tier-3 metadata lives 48 hours from ingestion. published_at cannot serve: an old
article discovered today would be purged on the next run and then re-ingested from
the feed again and again.
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
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX ix_articles_tier_ingested;
        ALTER TABLE articles DROP COLUMN ingested_at;
    """)
