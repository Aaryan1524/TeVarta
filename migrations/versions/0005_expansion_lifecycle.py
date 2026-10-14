"""Claimable lifecycle for T23 entity expansion requests.

Revision ID: 0005_expansion_lifecycle
Revises: 0004_onboarding_handoff
"""

from alembic import op

revision = "0005_expansion_lifecycle"
down_revision = "0004_onboarding_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows were written by T25 before any worker existed: they are pending.
    op.execute("""
        ALTER TABLE entity_expansion_requests
            ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'complete', 'failed')),
            ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            ADD COLUMN claimed_at TIMESTAMPTZ,
            ADD COLUMN completed_at TIMESTAMPTZ,
            ADD COLUMN last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 200),
            -- Bumped by every republish request, even mid-run, so a finishing worker can
            -- tell whether the graph changed under it.
            ADD COLUMN generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
            ADD CONSTRAINT ck_expansion_running_claimed
                CHECK (status <> 'running' OR claimed_at IS NOT NULL);
        CREATE INDEX ix_expansion_requests_claim
            ON entity_expansion_requests (status, requested_at);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX ix_expansion_requests_claim;
        ALTER TABLE entity_expansion_requests
            DROP CONSTRAINT ck_expansion_running_claimed,
            DROP COLUMN generation,
            DROP COLUMN last_error,
            DROP COLUMN completed_at,
            DROP COLUMN claimed_at,
            DROP COLUMN attempts,
            DROP COLUMN status;
    """)
