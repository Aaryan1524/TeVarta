"""Durable shared expansion handoff and prepared graph reads for T25.

Revision ID: 0004_onboarding_handoff
Revises: 0003_search_budget_reservations
"""

from alembic import op

revision = "0004_onboarding_handoff"
down_revision = "0003_search_budget_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE entity_expansion_requests (
            entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id) ON DELETE CASCADE,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE entity_graph_views (
            entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
            graph_json JSONB,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK ((status = 'ready') = (graph_json IS NOT NULL)),
            CHECK (graph_json IS NULL OR jsonb_typeof(graph_json) = 'object')
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE entity_graph_views")
    op.execute("DROP TABLE entity_expansion_requests")
