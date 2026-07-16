"""SQLite WAL + busy_timeout pragma wiring on the shared backend engine.

WAL matters because the Observing Pools refresh path holds a long write
transaction (scoring the whole pool) while concurrent discover writes and API
readers hit the same file. Under the default rollback-journal mode, readers
block the writer and vice versa, so a slow refresh starves discover writes with
"database is locked". WAL lets readers proceed against the last committed
snapshot while the single writer commits, and busy_timeout bounds the wait.
"""

import sqlite3

from sqlalchemy import create_engine, event, text

from app.backend.database.connection import set_sqlite_pragmas


def test_pragmas_applied_on_connect(tmp_path):
    """A throwaway file engine with the listener reports WAL + 30s busy_timeout.

    Uses a tmp_path file — never the real hedge_fund.db.
    """
    db_file = tmp_path / "t.db"
    engine = create_engine(f"sqlite:///{db_file}")
    event.listen(engine, "connect", set_sqlite_pragmas)

    with engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()

    engine.dispose()

    assert journal_mode == "wal"
    assert busy_timeout == 30000


def test_wal_is_durable_db_file_property(tmp_path):
    """WAL is written into the DB file header, so a later plain sqlite3
    connection to the same file still reports journal_mode='wal' even without
    the listener — proving the pragma persisted, not just per-connection state.
    """
    db_file = tmp_path / "t.db"
    engine = create_engine(f"sqlite:///{db_file}")
    event.listen(engine, "connect", set_sqlite_pragmas)

    # Open, apply pragmas, and materialize the file, then close.
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode")).scalar()
    engine.dispose()

    # A brand-new plain connection with NO listener still sees WAL.
    plain = sqlite3.connect(db_file)
    try:
        mode = plain.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        plain.close()

    assert mode == "wal"
