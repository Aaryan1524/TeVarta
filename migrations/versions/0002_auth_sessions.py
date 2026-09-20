"""Persist revocable refresh sessions for T11.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auth_sessions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            refresh_token_hash BYTEA NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (expires_at > created_at)
        );
        CREATE INDEX ix_auth_sessions_user ON auth_sessions(user_id);
        CREATE INDEX ix_auth_sessions_active ON auth_sessions(expires_at) WHERE revoked_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE auth_sessions")
