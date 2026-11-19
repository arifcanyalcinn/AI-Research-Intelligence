"""
LLMRegistry — selects the configured LLM backend (SDS §5.9, §6, §1.2).

Mirrors ``EmbeddingRegistry``. Discovery follows AD-02: the package
``__init__.py`` imports each backend module explicitly, and the registry scans
``BaseLLM.__subclasses__()``. Importing this module imports the package, so
the scan always sees a fully populated subclass list. D-004 applies: every
backend subclasses ``BaseLLM`` directly.

Exactly one backend is active per run — ``config.llm.backend`` names it — and
``get_backend()`` returns a fresh, unloaded instance for the §5.9 usage::

    registry = LLMRegistry(settings.llm)
    with registry.get_backend() as llm:
        result = llm.generate(rendered_prompt, params)

Wiring into the container and ``check-config`` is Stage 8 (ruling Q16).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from arip.exceptions import ConfigError
from arip.interfaces import BaseLLM

# Importing the package registers every backend as a BaseLLM subclass. Neither
# backend module imports torch or transformers at module level, so this costs
# nothing when the optional `llm` extra is absent.
import arip.backends.llm  # noqa: F401  isort:skip

if TYPE_CHECKING:
    from arip.config import LLMSettings

logger = structlog.get_logger(__name__)


class LLMRegistry:
    """Resolves ``config.llm.backend`` to a backend instance."""

    def __init__(self, config: LLMSettings) -> None:
        """Discover the backends and select the configured one.

        Raises:
            ConfigError: If ``config.backend`` names no registered backend
                         (§5.15: invalid configuration is fatal at startup).
        """
        self._backends: dict[str, type[BaseLLM]] = {
            cls.backend_id: cls for cls in BaseLLM.__subclasses__()
        }

        backend_id = config.backend
        if backend_id not in self._backends:
            available = ", ".join(sorted(self._backends)) or "none"
            raise ConfigError(
                f"Unknown LLM backend '{backend_id}'. "
                f"Available backends: {available}. "
                f"Set llm.backend in config/settings.yaml to one of these."
            )

        self._backend_id = backend_id
        self._model_name = config.model_name
        logger.info(
            "llm_backend_selected",
            backend_id=backend_id,
            model_name=config.model_name,
            available=sorted(self._backends),
        )

    def get_backend(self) -> BaseLLM:
        """Return a new, unloaded instance of the configured backend.

        A fresh instance per call: ``__exit__`` unloads the model, so a shared
        instance would be left unloaded but look loadable.
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
