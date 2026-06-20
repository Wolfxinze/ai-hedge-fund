"""add pool_locks table (Phase 8 — PRD §10 / X1 per-platform refresh lock)

Creates the claim-row table that serialises same-platform refreshes (the in-process APScheduler,
the CLI, and a future API all go through it). One row per platform_key; ``fence`` is the
generation token guarding against a stale-expiry steal lost-update (must-fix #5). Authoritative
schema source; ``create_all`` in app/backend/main.py coexists idempotently for dev.

Revision ID: b8f3c1a92d04
Revises: 7a1c2b3d4e5f
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8f3c1a92d04"
down_revision: Union[str, None] = "7a1c2b3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pool_locks",
        sa.Column("platform_key", sa.String(length=50), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("locked_by", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fence", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("platform_key"),
    )
    # All access is a PK lookup (WHERE platform_key = ?) — no secondary index needed.


def downgrade() -> None:
    op.drop_table("pool_locks")
