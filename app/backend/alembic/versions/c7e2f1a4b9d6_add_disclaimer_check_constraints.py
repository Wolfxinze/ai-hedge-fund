"""add non-empty CHECK constraints on disclaimer columns (Phase 11 — PRD §12/§20)

The disclaimer columns on ``opportunity_reports`` and ``serenity_research_records``
are NOT NULL, but NOT NULL admits an EMPTY string — only ``serialize_report``'s
``.strip()`` refused a blank disclaimer at runtime, so a direct DB/CLI write could
persist one. PRD §12/§20 require "serialization + DB CHECK"; this adds the missing
CHECK half (``length(trim(...)) > 0`` on both disclaimer + disclaimer_version) so a
blank disclaimer is impossible at the DB layer too.

SQLite cannot ALTER TABLE ADD CONSTRAINT, so the constraints are added via Alembic
batch mode (table recreate). Mirrors the model ``__table_args__`` CheckConstraints
so ``create_all`` and migrations agree.

Revision ID: c7e2f1a4b9d6
Revises: b8f3c1a92d04
Create Date: 2026-06-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c7e2f1a4b9d6"
down_revision: Union[str, None] = "b8f3c1a92d04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OPP = "ck_opportunity_reports_disclaimer_nonempty"
_SER = "ck_serenity_research_records_disclaimer_nonempty"
# Trim char-set = space + tab + newline + CR (bare SQLite trim() strips only ASCII
# space, so a tab/newline-only disclaimer would otherwise pass). Identical to the
# model __table_args__ so create_all and the migration agree.
_WS = "' ' || char(9) || char(10) || char(13)"
_COND = f"length(trim(disclaimer, {_WS})) > 0 AND length(trim(disclaimer_version, {_WS})) > 0"


def upgrade() -> None:
    with op.batch_alter_table("opportunity_reports", schema=None) as batch_op:
        batch_op.create_check_constraint(_OPP, _COND)
    with op.batch_alter_table("serenity_research_records", schema=None) as batch_op:
        batch_op.create_check_constraint(_SER, _COND)


def downgrade() -> None:
    with op.batch_alter_table("serenity_research_records", schema=None) as batch_op:
        batch_op.drop_constraint(_SER, type_="check")
    with op.batch_alter_table("opportunity_reports", schema=None) as batch_op:
        batch_op.drop_constraint(_OPP, type_="check")
