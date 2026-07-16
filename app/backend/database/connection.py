from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Get the backend directory path
BACKEND_DIR = Path(__file__).parent.parent
DATABASE_PATH = BACKEND_DIR / "hedge_fund.db"

# Database configuration - use absolute path
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"


def set_sqlite_pragmas(dbapi_conn, _connection_record):
    """Put every new SQLite connection into WAL journal mode with a 30s busy-wait.

    WAL (write-ahead logging): under the default rollback-journal mode a reader
    blocks the single writer and vice versa, so the long write transaction a pool
    refresh holds (scoring the whole pool) would starve a concurrent discover
    write and surface "database is locked". WAL keeps readers on the last
    committed snapshot while the one writer commits — readers never block the
    writer and the writer never blocks readers. busy_timeout=30000ms mirrors the
    connect_args timeout so a contending writer waits (bounded) instead of
    immediately raising, which is what the PoolLock claim protocol relies on
    (PRD §10). WAL is a durable DB-file property, so setting it once sticks.
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


# Create SQLAlchemy engine
engine = create_engine(
    DATABASE_URL,
    # check_same_thread=False: the Phase-8 in-process APScheduler runs jobs in worker
    # threads that share this engine. timeout=30: SQLite busy-wait (seconds) so concurrent
    # writers (scheduler thread + a CLI/API refresh) serialize instead of immediately raising
    # "database is locked" — the bounded retry the PoolLock claim protocol relies on (PRD §10).
    # See set_sqlite_pragmas below: WAL is what actually lets a long refresh transaction and
    # concurrent discover writes coexist instead of one starving the other under rollback-journal.
    connect_args={"check_same_thread": False, "timeout": 30},
)

# Apply WAL + busy_timeout on every new DBAPI connection to the shared engine.
event.listen(engine, "connect", set_sqlite_pragmas)

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class for models
Base = declarative_base()

# Dependency for FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close() 