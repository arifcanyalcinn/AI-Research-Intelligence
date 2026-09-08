"""
SentenceTransformersBackend — the real CPU-only embedding backend (SDS §5.6).

Wraps ``sentence_transformers.SentenceTransformer`` with the lifecycle §5.6
specifies: lazy load on ``__enter__``, unload on ``__exit__``, and
``embed(texts) -> np.ndarray`` in between.

**CPU-only, by explicit code.** §5.6: "This is enforced by setting
``device='cpu'`` in the ``SentenceTransformer`` constructor — not by convention
but by explicit code." AD-06 makes the same point at the architecture level: the
embedder runs on CPU, the LLM on GPU, never simultaneously. The ``device="cpu"``
argument below is that enforcement, and `test_device_is_cpu` asserts it.

**Imports are function-local, deliberately.** ``sentence_transformers`` and
``numpy`` arrive only with the optional ``embedding`` extra, and this module is
imported by the package ``__init__`` whenever anything touches the registry
(AD-02). A module-level import here would make the extra mandatory and
contradict ``pyproject.toml``'s statement that the source and ranking layers
install without torch. §5.6's lazy-load requirement points the same way; this is
the structural reason it is not optional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import structlog

from arip.exceptions import EmbeddingError
from arip.interfaces import BaseEmbedder

if TYPE_CHECKING:
    import numpy as np

logger = structlog.get_logger(__name__)

EMBEDDING_DEVICE = "cpu"
"""Device passed to the SentenceTransformer constructor.

Not configurable, by design. SDS §5.6 and AD-06 make CPU-only a hard constraint
rather than a preference: the LLM owns the GPU, and the two must never share
VRAM. Making this a config field would turn an architectural invariant into an
operator mistake waiting to happen.
"""


class SentenceTransformersBackend(BaseEmbedder):
    """CPU-only embeddings via ``sentence-transformers``.

    Direct subclass of ``BaseEmbedder`` per D-004 — ``__subclasses__()`` does
    not recurse, so an intermediate base shared with ``StubEmbedder`` would hide
    both from the registry. The structural similarity between the two is
    accepted and intentional.
    """

    backend_id: ClassVar[str] = "sentence_transformers"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        """
        Args:
            model_name: Model to load, from ``config.embeddings.model_name``.
                        Default matches §5.6 (all-MiniLM-L6-v2, 384-dim, ~90 MB).
        """
        self._model_name = model_name
        self._model: object | None = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def embedding_dim(self) -> int:
        """Vector width, read from the loaded model.

        Raises:
            RuntimeError: If the model is not loaded. The dimension is a
                          property of the model, so it cannot be known before
                          ``__enter__``.
        """
        if self._model is None:
            raise RuntimeError(
                "embedding_dim is unavailable before the model is loaded. "
                "Use `with embedder:` first (SDS §5.6)."
            )
        return int(self._model.get_sentence_embedding_dimension())

    @property
    def model_name(self) -> str:
        """Name recorded on embedded items as ``embedding_model_name``."""
        return self._model_name

    # ------------------------------------------------------------------
    # Lifecycle — SDS §5.6 context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> SentenceTransformersBackend:
        """Load the model into CPU RAM.

        Raises:
            EmbeddingError: If the model cannot be loaded — most commonly a
                            failed first-run download. §5.6 requires the message
                            to tell the operator to pre-download the model.
        """
        # Optional dependency: see module docstring.
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        try:
            self._model = SentenceTransformer(self._model_name, device=EMBEDDING_DEVICE)
        except Exception as exc:
            raise EmbeddingError(
                f"Could not load embedding model '{self._model_name}'. "
                f"The first run downloads it (~90 MB) from Hugging Face; if the "
                f"machine is offline or the download failed, pre-download the "
                f"model and retry. Original error: {exc}"
            ) from exc

        logger.info(
            "embedder_loaded",
            backend_id=self.backend_id,
            model_name=self._model_name,
            device=EMBEDDING_DEVICE,
        )
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: object,
    ) -> None:
        """Unload the model, releasing CPU RAM.

        Dropping the reference is the whole mechanism — the model lives in CPU
        RAM, so there is no VRAM cache to clear. The LLM backend's ``__exit__``
        additionally calls ``torch.cuda.empty_cache()`` (§5.9); this one has
        nothing equivalent to do.
        """
        self._model = None
        logger.info("embedder_unloaded", backend_id=self.backend_id)

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def embed(self, texts: list[str]) -> np.ndarray:
        """Encode a batch of texts.

        Args:
            texts: Texts to embed. An empty list returns an empty
                   ``(0, embedding_dim)`` array without calling the model.

        Returns:
            float32 array of shape ``(len(texts), embedding_dim)``.

        Raises:
            RuntimeError: If called outside the context manager.
            EmbeddingError: If encoding fails — including CPU-RAM exhaustion,
                            which §5.6 lists as a failure mode.
        """
        import numpy as np  # noqa: PLC0415 - optional dependency, see module docstring

        if self._model is None:
            raise RuntimeError(
                "SentenceTransformersBackend.embed() called outside its context "
                "manager. Use `with embedder:` before embedding (SDS §5.6)."
            )

        if not texts:
            return np.empty((0, self.embedding_dim), dtype=np.float32)

        try:
            vectors = self._model.encode(
                texts,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        except MemoryError as exc:
            raise EmbeddingError(
                f"Ran out of CPU RAM embedding {len(texts)} texts with "
                f"'{self._model_name}'. Reduce the batch size or free memory."
            ) from exc
        except Exception as exc:
            raise EmbeddingError(
                f"Embedding failed for {len(texts)} texts with "
                f"'{self._model_name}': {exc}"
            ) from exc

        return np.asarray(vectors, dtype=np.float32)
