"""The Alembic migration must apply, round-trip, and match the ORM models.

The rest of the suite builds schema via ``create_all``, so it is BLIND to
migration drift — a migration missing a column (e.g. the fetch_errors/degraded
columns the loud-fail recording writes to) would ship with every test green and
then fail at runtime in production (which runs migrations, not create_all).
PRD P2 / review Test-Gap-2.
"""

import os
import tempfile

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import src.storage.models as m

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_FEATURE_TABLES = {
    "candidate_securities",
    "innovation_platforms",
    "observation_pool_entries",
    "pool_refresh_runs",
    "serenity_research_records",
    "evidence_references",
    "monitor_configs",
    "opportunity_reports",
}


def _cfg(db_url: str) -> Config:
    cfg = Config(os.path.join(_REPO, "app/backend/alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_REPO, "app/backend/alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_migration_applies_roundtrips_and_matches_orm():
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{os.path.join(d, 't.db')}"
        cfg = _cfg(url)

        command.upgrade(cfg, "head")

        insp = inspect(create_engine(url))
        names = set(insp.get_table_names())
        assert _FEATURE_TABLES <= names, f"missing tables: {_FEATURE_TABLES - names}"

        # ORM column names must match migrated column names for each feature table.
        for t in _FEATURE_TABLES:
            orm_cols = {c.name for c in m.Base.metadata.tables[t].columns}
            migrated_cols = {c["name"] for c in insp.get_columns(t)}
            assert orm_cols == migrated_cols, f"{t}: ORM↔migration drift {orm_cols ^ migrated_cols}"

        # Downgrade removes exactly the feature tables (proves downgrade() is correct).
        command.downgrade(cfg, "-1")
        after = set(inspect(create_engine(url)).get_table_names())
        assert not (_FEATURE_TABLES & after), f"left behind after downgrade: {_FEATURE_TABLES & after}"
