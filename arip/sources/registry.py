"""
SourceRegistry — discovers and holds all enabled source plugin instances.

Discovery relies on ``BaseSource.__subclasses__()``, which is populated when
``arip.sources`` is imported (that package's ``__init__.py`` explicitly imports
every source class).  The registry must therefore be constructed *after* the
sources package has been imported.

The registry is stateless after construction.  Construct it once at startup
(in ``container.py``) and inject it into the collection stage.

Failure policy: if a source's ``__init__`` raises, that source is skipped and
logged at ERROR level.  The remaining sources are unaffected.

Usage::

    import arip.sources  # registers all subclasses via __init__.py imports
    registry = SourceRegistry(config)
    for source in registry.get_active_sources():
        payloads = source.fetch()
"""

from __future__ import annotations

import structlog

from arip.config import AppSettings
from arip.interfaces import BaseSource

logger = structlog.get_logger(__name__)


class SourceRegistry:
    """Holds all enabled and successfully initialised source plugin instances.

    Attributes:
        _sources: Mapping of ``source_id`` → ``BaseSource`` instance for every
                  source that is enabled in config and whose ``__init__`` did
                  not raise.
    """

    def __init__(self, config: AppSettings) -> None:
        """Discover and initialise all enabled source plugins.

        For each ``BaseSource`` subclass found via ``__subclasses__()``:

        1. Look up its config block in ``config.sources`` by ``source_id``.
        2. If the config block exists and ``enabled=False``, skip the source.
        3. Instantiate; if ``__init__`` raises, log ERROR and skip — the
           pipeline continues with remaining sources.

        Args:
            config: Fully validated ``AppSettings`` instance.
        """
        self._sources: dict[str, BaseSource] = {}

        for cls in BaseSource.__subclasses__():
            source_id: str = cls.source_id
            source_config = getattr(config.sources, source_id, None)

            # Respect the enabled flag; absence of a config block is allowed —
            # the source uses its own built-in defaults.
            if source_config is not None and not source_config.enabled:
                logger.info("source_disabled", source_id=source_id)
                continue

            try:
                instance = cls(source_config)
                self._sources[source_id] = instance
                logger.info("source_registered", source_id=source_id)
            except Exception:
                # One bad source must not prevent the others from loading.
                logger.error(
                    "source_init_failed",
                    source_id=source_id,
                    exc_info=True,
                )

    def get_active_sources(self) -> list[BaseSource]:
        """Return all successfully initialised, enabled source plugins.

        Returns:
            List of ``BaseSource`` instances in registration order.  Empty if
            no sources are enabled or all failed to initialise.
        """
        return list(self._sources.values())

    def get_source(self, source_id: str) -> BaseSource | None:
        """Return a specific source by its ``source_id``, or ``None``.

        Args:
            source_id: The ``source_id`` class attribute of the desired plugin,
                       e.g. ``"arxiv"``.

        Returns:
            The ``BaseSource`` instance, or ``None`` if absent or disabled.
        """
        return self._sources.get(source_id)
