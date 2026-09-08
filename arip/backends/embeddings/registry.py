"""
EmbeddingRegistry — selects the configured embedding backend (SDS §6, §1.2).

Discovery follows the rule AD-02 sets for every plugin family: the package
``__init__.py`` imports each backend module explicitly, and the registry then
scans ``BaseEmbedder.__subclasses__()``. Importing this module imports the
package, so the scan always sees a fully populated subclass list.

D-004 applies: ``__subclasses__()`` returns **direct** subclasses only. Every
backend must therefore subclass ``BaseEmbedder`` directly.

Difference from ``SourceRegistry``, and why: the source registry holds *every*
enabled source and the collection stage iterates them all. Exactly one embedding
backend is active per run — ``config.embeddings.backend`` names it — so this
registry resolves one and returns it. That mirrors §5.9's
``llm_registry.get_backend()``.

Usage::

    registry = EmbeddingRegistry(settings.embeddings)
    with registry.get_backend() as embedder:
        vectors = embedder.embed(["some text"])
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from arip.exceptions import ConfigError
from arip.interfaces import BaseEmbedder

# Importing the package registers every backend as a BaseEmbedder subclass.
# Neither backend module imports numpy or sentence_transformers at module
# level, so this costs nothing when the optional `embedding` extra is absent.
import arip.backends.embeddings  # noqa: F401  isort:skip

if TYPE_CHECKING:
    from arip.config import EmbeddingSettings

logger = structlog.get_logger(__name__)


class EmbeddingRegistry:
    """Resolves ``config.embeddings.backend`` to a backend instance."""

    def __init__(self, config: EmbeddingSettings) -> None:
        """Discover the backends and select the configured one.

        Args:
            config: The ``embeddings`` block of ``AppSettings``.

        Raises:
            ConfigError: If ``config.backend`` names no registered backend.
                         §5.15 makes invalid configuration fatal at startup
                         rather than at first use, so this fails here — while
                         the message can still name the alternatives — instead
                         of part-way through a run.
        """
        self._backends: dict[str, type[BaseEmbedder]] = {
            cls.backend_id: cls for cls in BaseEmbedder.__subclasses__()
        }

        backend_id = config.backend
        if backend_id not in self._backends:
            available = ", ".join(sorted(self._backends)) or "none"
            raise ConfigError(
                f"Unknown embedding backend '{backend_id}'. "
                f"Available backends: {available}. "
                f"Set embeddings.backend in config/settings.yaml to one of these."
            )

        self._backend_id = backend_id
        self._model_name = config.model_name
        logger.info(
            "embedding_backend_selected",
            backend_id=backend_id,
            model_name=config.model_name,
            available=sorted(self._backends),
        )

    def get_backend(self) -> BaseEmbedder:
        """Return a new instance of the configured backend.

        A fresh instance per call, not a shared one: ``BaseEmbedder`` is a
        context manager whose ``__exit__`` unloads the model, so a shared
        instance reused across passes would be left in an unloaded state that
        looks loadable. The embedding pass is entered once per run.

        Returns:
            An unloaded backend. Use it as a context manager (§5.6).
        """
        return self._backends[self._backend_id](model_name=self._model_name)

    @property
    def backend_id(self) -> str:
        """Identifier of the selected backend."""
        return self._backend_id

    @property
    def available_backends(self) -> list[str]:
        """Every discovered backend id, sorted. Diagnostic use."""
        return sorted(self._backends)
