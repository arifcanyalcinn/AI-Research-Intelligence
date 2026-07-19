"""
Alembic migration environment.

Reads the database URL from AppSettings (config/settings.yaml + env vars)
so there is a single source of truth for the DB URL — no duplication between
alembic.ini and application config.

To run migrations:
    alembic upgrade head          # apply all pending migrations
    alembic downgrade -1          # undo the last migration
    alembic revision --autogenerate -m "description"   # generate a new migration
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Make the arip package importable from the project root.
# ---------------------------------------------------------------------------
# When Alembic runs, cwd is the project root (same directory as alembic.ini).
# Add the project root to sys.path so `from arip...` imports work.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from arip.config import load_settings  # noqa: E402
from arip.db.models import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Alembic Config object
# ---------------------------------------------------------------------------
config = context.config

# Set up Python logging from alembic.ini [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# SQLAlchemy metadata for --autogenerate support.
target_metadata = Base.metadata


def get_url() -> str:
    """Load the database URL from AppSettings.

    Falls back to an in-memory SQLite DB if settings cannot be loaded
    (e.g., during CI where config/settings.yaml may not exist).
    """
    try:
        settings = load_settings()
        return settings.database.url
    except Exception:
        return "sqlite:///data/arip.db"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Generates SQL scripts without connecting to the database.
    Useful for reviewing migrations before applying them.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connected to the database)."""
    url = get_url()

    # Override the sqlalchemy.url from alembic.ini with the URL from settings.
    connectable = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
