"""Reserve the shared monthly search budget before provider dispatch.

Revision ID: 0003_search_budget_reservations
Revises: 0002
"""

from alembic import op

revision = "0003_search_budget_reservations"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE search_query_reservations (
            request_id UUID PRIMARY KEY,
            period_start DATE NOT NULL,
            reserved_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_search_query_reservations_period
            ON search_query_reservations(period_start);
        CREATE TABLE search_budget_alerts (
            period_start DATE NOT NULL,
            alert_name TEXT NOT NULL,
            emitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (period_start, alert_name)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE search_budget_alerts")
    op.execute("DROP TABLE search_query_reservations")
