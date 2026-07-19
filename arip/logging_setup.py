"""
Structured logging setup for ARIP using structlog.

Configures:
  - JSON renderer in production (sys.stdout is not a tty)
  - Colorized console renderer in development (sys.stdout is a tty)
  - Rotating file handler for persistent log storage
  - Log level from config

All pipeline code should use:
    import structlog
    logger = structlog.get_logger(__name__)

Per-item context must use the context manager pattern to prevent bleed:
    with structlog.contextvars.bound_contextvars(
        run_id=run_id,
        stage="generation",
        item_id=item.id,
        source_id=item.source_id,
    ):
        content = generator.generate(item)
    # Context is cleared here — no bleed to the next item.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from arip.config import LoggingSettings


def setup_logging(config: LoggingSettings) -> None:
    """Configure structlog and the Python logging backend.

    Must be called once at application startup, before any loggers are used.
    Calling it multiple times is safe (idempotent for the structlog config).

    Args:
        config: The LoggingSettings model from AppSettings.
    """
    log_level = getattr(logging, config.level.upper(), logging.INFO)
    is_tty = sys.stdout.isatty()

    # ------------------------------------------------------------------
    # Shared processors: applied to every log record regardless of renderer.
    # ------------------------------------------------------------------
    shared_processors: list = [
        # Inject context variables bound via bound_contextvars().
        structlog.contextvars.merge_contextvars,
        # Add the logger name (module path).
        structlog.stdlib.add_logger_name,
        # Add the log level string.
        structlog.stdlib.add_log_level,
        # Add a UTC timestamp in ISO 8601 format.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Format exception tracebacks into the log record.
        structlog.processors.format_exc_info,
        # Render callsite info (file, function, line) at DEBUG level.
        structlog.processors.CallsiteParameterAdder(
            [
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            ]
        ),
    ]

    # ------------------------------------------------------------------
    # Configure structlog.
    # ------------------------------------------------------------------
    structlog.configure(
        processors=shared_processors
        + [
            # Bridge to stdlib logging so our file handler receives records.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ------------------------------------------------------------------
    # Configure stdlib logging (structlog uses it as the output backend).
    # ------------------------------------------------------------------
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    # Remove any handlers added by previous calls or by imports.
    root_logger.handlers.clear()

    # Choose renderer based on environment.
    if is_tty:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    formatter = structlog.stdlib.ProcessorFormatter(
        # These processors run only on stdlib records (i.e., records not
        # originating from structlog — e.g., SQLAlchemy, httpx).
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    # ------------------------------------------------------------------
    # Stderr handler: always outputs ERROR+ to stderr.
    # In development (tty), also outputs all levels.
    # ------------------------------------------------------------------
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR if not is_tty else log_level)
    stderr_handler.setFormatter(formatter)
    root_logger.addHandler(stderr_handler)

    # ------------------------------------------------------------------
    # File handler: rotating, JSON format, all levels.
    # ------------------------------------------------------------------
    log_file_path = Path(config.log_file)
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_file_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)

    # File always gets JSON format regardless of tty status.
    json_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    file_handler.setFormatter(json_formatter)
    root_logger.addHandler(file_handler)

    # Quiet noisy third-party loggers.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Emit a startup log to confirm logging is working.
    startup_logger = structlog.get_logger("arip.logging")
    startup_logger.info(
        "logging_configured",
        level=config.level,
        log_file=str(log_file_path),
        json_format=config.json_format,
        renderer="console" if is_tty else "json",
    )
