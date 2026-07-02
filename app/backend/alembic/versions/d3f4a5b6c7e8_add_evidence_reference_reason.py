"""add nullable evidence_references.reason column (Phase 11 — PRD §11.5 durability)

``classify_reference`` already computes a coarse, deterministic ``reason`` explaining
why a reference did/didn't substantiate a claim (``figure_missing``, ``keyword_stuffing``,
``ok``, …). Until now it was observability-only — logged then dropped. This persists it
onto ``evidence_references.reason`` so a withheld grade is auditable after the fact.

Audit metadata only: nullable, backfill-free (existing rows stay NULL), and NOT surfaced
through ``serialize_serenity`` or the API — it never feeds the deterministic grade.

SQLite cannot ALTER TABLE DROP COLUMN, so the downgrade uses Alembic batch mode (table
recreate). Add is done in batch too for symmetry; both round-trip clean on SQLite.

Revision ID: d3f4a5b6c7e8
Revises: c7e2f1a4b9d6
Create Date: 2026-07-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3f4a5b6c7e8"
down_revision: Union[str, None] = "c7e2f1a4b9d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("evidence_references", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("evidence_references", schema=None) as batch_op:
        batch_op.drop_column("reason")
