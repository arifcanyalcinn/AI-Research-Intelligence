"""
Database engine and session management for ARIP.

Single entry point for all database infrastructure:
  - create_engine() with WAL mode enabled via event listener
  - session_scope() context manager for transactional units of work
  - init_db() to create all tables (used in tests; production uses Alembic)

WAL (Write-Ahead Logging) is enabled at the connection level so that:
  - The Telegram background thread can read the DB while the main pipeline writes.
  - Readers and writers don't block each other.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import structlog
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from arip.db.models import Base

logger = structlog.get_logger(__name__)


def build_engine(database_url: str) -> Engine:
    """Create and configure the SQLAlchemy engine.

    Enables WAL journal mode for SQLite via a connection event listener.
    WAL mode persists across connections once set, but setting it on every
    new connection is idempotent and ensures correctness if the DB file is
    recreated.

    Args:
        database_url: SQLAlchemy connection string, e.g. 'sqlite:///data/arip.db'

    Returns:
        Configured Engine instance.
    """
    engine = create_engine(
        database_url,
        # echo=False: don't log every SQL statement in production.
        # Set to True (or use ARIP_LOGGING__LEVEL=DEBUG) for debugging.
        echo=False,
        # pool_pre_ping: verify connections are alive before using them.
        pool_pre_ping=True,
    )

    # Enable WAL mode for SQLite.
    # This listener fires once per new connection, which is fine — the PRAGMA
    # is idempotent and fast.
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def set_wal_mode(conn, _record) -> None:  # type: ignore[type-arg]
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")  # Safe with WAL; faster writes.
            conn.execute("PRAGMA foreign_keys=ON")      # Enforce FK constraints.

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a session factory bound to the given engine.

    Returns:
        A callable that produces Session objects when called.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations.

    Commits on clean exit, rolls back on any exception, and always closes
    the session. This is the canonical way to acquire a session in ARIP:

        with session_scope(session_factory) as session:
            repo = ItemRepository(session)
            item = repo.get_by_id(1)

    Args:
        session_factory: The sessionmaker produced by build_session_factory().

    Yields:
        An active Session object.

    Raises:
        Re-raises any exception after rollback.
    """
    session: Session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine) -> None:
    """Create all tables from ORM metadata.

    This is used in tests and for first-time setup without Alembic.
    In production, always use `alembic upgrade head` instead — it handles
    incremental migrations correctly.

    Args:
        engine: The engine returned by build_engine().
    """
    Base.metadata.create_all(engine)
    logger.info("database_initialized", tables=list(Base.metadata.tables.keys()))


def check_db_connection(engine: Engine) -> bool:
    """Verify the database is reachable with a trivial query.

    Used by the startup health check to fail fast if the DB is unreachable.

    Returns:
        True if the database is accessible.

    Raises:
        Exception: If the database cannot be reached (let the caller handle it).
    """
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
