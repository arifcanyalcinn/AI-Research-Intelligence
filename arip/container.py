"""
Application container — constructor injection factory functions.

This is NOT a DI container or service locator. It is plain factory code
that wires together the components defined elsewhere.

All objects are constructed here and passed to their dependents via __init__
parameters. Nothing reaches into this module at runtime — it runs once at
startup and hands off fully-wired objects to main.py.

Phase 0: only config, logging, and DB wiring.
Phase 1+: source registry, pipeline orchestrator, scheduler, etc. are added here.

Target size: ~50-100 lines of straightforward factory code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from arip.config import AppSettings, load_settings
from arip.db.database import build_engine, build_session_factory, check_db_connection
from arip.logging_setup import setup_logging


@dataclass
class AppComponents:
    """Fully-wired application components, ready for use.

    Constructed once by build_app_components() and held for the lifetime of
    the process. All components share the same settings and session factory.
    """

    settings: AppSettings
    engine: Engine
    session_factory: sessionmaker[Session]


def build_app_components(
    yaml_path: Path = Path("config/settings.yaml"),
) -> AppComponents:
    """Load config, set up logging, initialize DB, return wired components.

    This is the application startup sequence. Call it once from main.py.

    Args:
        yaml_path: Path to the YAML config file. Override in tests.

    Returns:
        AppComponents with all Phase 0 infrastructure initialized.

    Raises:
        ConfigError: If settings are invalid.
        Exception: If the database cannot be reached (propagated as-is so
                   the caller can log a CRITICAL message and exit).
    """
    # Step 1: Load and validate all configuration.
    # This raises ConfigError immediately if anything is wrong.
    settings = load_settings(yaml_path)

    # Step 2: Set up structured logging.
    # Must happen before any logger calls so all subsequent output is formatted.
    setup_logging(settings.logging)

    # Step 3: Ensure runtime directories exist.
    _ensure_runtime_dirs(settings)

    # Step 4: Initialize the database engine with WAL mode.
    engine = build_engine(settings.database.url)

    # Step 5: Verify the database is reachable.
    # Fails fast here rather than mid-pipeline.
    check_db_connection(engine)

    # Step 6: Build the session factory.
    session_factory = build_session_factory(engine)

    return AppComponents(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
    )


def _ensure_runtime_dirs(settings: AppSettings) -> None:
    """Create runtime directories if they don't exist.

    data/ — holds the SQLite DB and ANN index.
    logs/ — holds rotating log files.
    """
    import logging
    from pathlib import Path as _Path

    log = logging.getLogger("arip.container")

    dirs = [
        _Path("data"),
        _Path(_Path(settings.logging.log_file).parent),
        _Path(_Path(settings.dedup.ann_index_path).parent),
    ]
    for d in dirs:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            log.debug(f"Created directory: {d}")
