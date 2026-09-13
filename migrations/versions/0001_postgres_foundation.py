"""Create the frozen MVP persistence foundation.

Revision ID: 0001
Revises: None
"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(Path(__file__).with_suffix(".sql").read_text())


def downgrade() -> None:
    # No CASCADE: unknown downstream objects must prevent destructive rollback.
    for table in (
        "usage_records",
        "event_claims",
        "demand_log",
        "resolution_misses",
        "feedback",
        "notification_log",
        "score_distribution",
        "user_feed",
        "why_cache",
        "event_summaries",
        "event_entities",
        "event_articles",
        "events",
        "recall_audit",
        "quarantine",
        "article_entities",
        "articles",
        "sources",
        "entity_aliases",
        "user_seeds",
        "entities",
        "devices",
        "users",
    ):
        op.execute(f'DROP TABLE "{table}"')
    op.execute("DROP FUNCTION tevarta_valid_components(jsonb)")
    # pgvector is shared infrastructure and is deliberately retained.
