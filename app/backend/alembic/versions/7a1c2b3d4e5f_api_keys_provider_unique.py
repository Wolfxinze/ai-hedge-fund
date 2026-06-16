"""api_keys.provider unique index (reconcile model<->migration drift)

The ApiKey model declares ``provider`` as a unique index, but the original
add_api_keys_table migration created it non-unique. This reconciles the safe,
clearly-correct part of the drift surfaced by autogenerate (issue #7). The FK
constraints on the hedge_fund_flow tables are intentionally NOT touched here
(SQLite cannot ALTER-add a FK without fragile batch ops) — see #7.

Revision ID: 7a1c2b3d4e5f
Revises: 58e25bfcb251
Create Date: 2026-06-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "7a1c2b3d4e5f"
down_revision: Union[str, None] = "58e25bfcb251"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f("ix_api_keys_provider"), table_name="api_keys")
    op.create_index(op.f("ix_api_keys_provider"), "api_keys", ["provider"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_api_keys_provider"), table_name="api_keys")
    op.create_index(op.f("ix_api_keys_provider"), "api_keys", ["provider"], unique=False)
