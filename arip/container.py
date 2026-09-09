"""
Application container — constructor injection factory functions.

This is NOT a DI container or service locator. It is plain factory code
that wires together the components defined elsewhere.

All objects are constructed here and passed to their dependents via __init__
parameters. Nothing reaches into this module at runtime — it runs once at
startup and hands off fully-wired objects to main.py.

Phase 0: config, logging, and DB wiring.
Batch 7: source registry and pipeline orchestrator (SDS §8.2).
Batch 8: ranking scorer.
Batch 9: embedding registry and semantic deduplicator factory.
Later batches: scheduler, LLM registry, reviewer, publishers.

Target size: ~50-100 lines of straightforward factory code.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

# Importing the registry also imports the arip.backends.embeddings package,
# whose __init__.py registers every backend as a BaseEmbedder subclass (D-004).
# EmbeddingRegistry must not be constructed before that has happened.
from arip.backends.embeddings.registry import EmbeddingRegistry
from arip.config import AppSettings, load_settings
from arip.db.database import build_engine, build_session_factory, check_db_connection
from arip.dedup.semantic import SemanticDeduplicator
from arip.logging_setup import setup_logging
from arip.pipeline.orchestrator import PipelineOrchestrator
from arip.ranking.scorer import Scorer

# Importing the registry also imports the arip.sources package, whose
# __init__.py registers every source class as a BaseSource subclass (D-002).
# SourceRegistry must not be constructed before that has happened.
from arip.sources.registry import SourceRegistry


@dataclass
class AppComponents:
    """Fully-wired application components, ready for use.

    Constructed once by build_app_components() and held for the lifetime of
    the process. All components share the same settings and session factory.
    """

    settings: AppSettings
    engine: Engine
    session_factory: sessionmaker[Session]
    source_registry: SourceRegistry
    embedding_registry: EmbeddingRegistry
    orchestrator: PipelineOrchestrator


def build_app_components(
    yaml_path: Path = Path("config/settings.yaml"),
) -> AppComponents:
    """Load config, set up logging, initialize DB, return wired components.

    This is the application startup sequence. Call it once from main.py.

    Args:
        yaml_path: Path to the YAML config file. Override in tests.

    Returns:
        AppComponents with the infrastructure and pipeline objects wired.

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

    # Step 7: Discover and instantiate the enabled source plugins.
    # A source whose __init__ raises is logged and skipped (SDS §5.2).
    source_registry = SourceRegistry(settings)

    # Step 8: Build the ranking scorer from the validated ranking config.
    # It is stateless, so one instance serves every run.
    scorer = Scorer(settings.ranking)

    # Step 9: Select the embedding backend named by config.embeddings.backend.
    # Constructing the registry validates the name and fails fast with a
    # ConfigError; no model is loaded here, and none is loaded until the
    # embedding stage enters the backend it hands out (§5.6, AD-06).
    embedding_registry = EmbeddingRegistry(settings.embeddings)

    # Step 10: Bind the dedup configuration to a deduplicator factory.
    #
    # A factory, not an instance, because SemanticDeduplicator needs the ANN
    # index width and the only truthful source of that is the loaded embedding
    # model — which is not loaded at startup and must not be (AD-06). Binding
    # the config here keeps config resolution in this module: the stage calls
    # factory(embedding_dim=...) and never sees a settings object.
    deduplicator_factory = partial(SemanticDeduplicator, config=settings.dedup)

    # Step 11: Wire the orchestrator with everything a run needs.
    # It receives constructed collaborators, single values and pre-bound
    # factories, never the settings object itself.
    orchestrator = PipelineOrchestrator(
        registry=source_registry,
        session_factory=session_factory,
        scorer=scorer,
        min_score=settings.ranking.min_score,
        embedder_factory=embedding_registry.get_backend,
        deduplicator_factory=deduplicator_factory,
    )

    return AppComponents(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        source_registry=source_registry,
        embedding_registry=embedding_registry,
        orchestrator=orchestrator,
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
