"""
StubEmbedder — a deterministic embedding backend that loads no model.

SDS §5.6 Testing Strategy: "Use a mock embedder that returns random unit
vectors of the correct dimension. The deduplication logic is tested against
this mock without loading any real model."

This backend exists so that every test in the batch — and every pipeline run an
operator wants to exercise without a 90 MB model download — can run the
embedding path end to end. It is selected by setting
``embeddings.backend: "stub"`` in ``config/settings.yaml``.

**Deterministic, not random.** §5.6 says "random unit vectors". This
implementation derives each vector from a SHA-256 hash of the input text, so
the vectors are uniformly distributed and uncorrelated with anything
meaningful — random in every respect that matters to a similarity test — but
identical text always produces an identical vector. Without that property, no
test could assert that two items with the same text are semantic duplicates,
which is the behaviour §5.7's testing strategy requires ("Test `is_duplicate()`
with known similar pairs").

**Every stub-embedded row is marked in the database.** ``model_name`` returns
``"stub:<model>"`` — always prefixed, never the bare configured name. Stage 4
writes that value to ``items.embedding_model_name``, and §5.7's reconciliation
selects on ``embedding_computed_at IS NOT NULL``, which cannot tell a stub
vector from a real one. Without the prefix, a run with
``embeddings.backend: "stub"`` would fill the ANN index with stub vectors
labelled ``all-MiniLM-L6-v2``, and switching to the real backend would leave
the data poisoned with nothing in the row to detect it. The prefix is that
detection; the suffix preserves which model the stub stood in for.

The prefix cannot be bypassed by configuration: ``EmbeddingRegistry`` passes
``config.model_name`` to whichever backend it selects, so the constructor
argument is always the configured model, and the marking is applied on the way
out rather than on the way in.

``numpy`` is imported inside ``embed()`` rather than at module level. This
module is imported by the package ``__init__`` (AD-02), which runs whenever
anything touches the registry, and ``numpy`` arrives only with the optional
``embedding`` extra.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, ClassVar

import structlog

from arip.interfaces import BaseEmbedder

if TYPE_CHECKING:
    import numpy as np

logger = structlog.get_logger(__name__)

STUB_EMBEDDING_DIM = 384
"""Dimension of the stub's vectors.

Matches all-MiniLM-L6-v2 (SDS §5.6) so that swapping between this backend and
the real one changes no downstream assumption — in particular, an ANN index
built by one has the same width as an index built by the other.

A module constant rather than configuration, per D-005: the SDS types
``EmbeddingSettings`` with ``backend`` and ``model_name`` only, and adding a
dimension field would change the frozen config schema.
"""

STUB_MODEL_NAME_PREFIX = "stub:"
"""Prefix on every ``embedding_model_name`` this backend produces.

Exported as a constant so that anything needing to separate stub rows from real
ones — a reconciliation query, an operator's SQL, a future re-embed command —
matches on this name rather than on a literal string copied by hand.
"""


class StubEmbedder(BaseEmbedder):
    """Deterministic pseudo-random embeddings with no model and no I/O.

    Direct subclass of ``BaseEmbedder`` per D-004 — ``__subclasses__()`` does
    not recurse, so an intermediate base shared with
    ``SentenceTransformersBackend`` would hide both from the registry.
    """

    backend_id: ClassVar[str] = "stub"

    def __init__(self, model_name: str = "stub") -> None:
        """
        Args:
            model_name: The model this stub stands in for — in production
                        always ``config.embeddings.model_name``, since
                        ``EmbeddingRegistry`` passes it to every backend. The
                        value loads nothing; it is recorded, prefixed, by the
                        ``model_name`` property.

                        The default is deliberately **not** the real backend's
                        ``all-MiniLM-L6-v2``. A caller who named no model stood
                        in for nothing, and recording a real model name there
                        would be the same shape of untruth this prefix exists
                        to prevent — a name asserting something that did not
                        happen. A bare ``StubEmbedder()`` therefore reports
                        ``"stub:stub"``: degenerate, and accurate. Production
                        never reaches it.
        """
        self._model_name = model_name
        self._loaded = False

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def embedding_dim(self) -> int:
        """Vector width. Constant — no model is consulted."""
        return STUB_EMBEDDING_DIM

    @property
    def model_name(self) -> str:
        """Name recorded on embedded items — always prefixed ``stub:``.

        Returns:
            ``"stub:<configured model>"``. The prefix is not optional and not
            configurable: it is the only thing in the row that distinguishes a
            stub vector from a real one (see the module docstring), and §5.7's
            reconciliation query cannot make that distinction on its own.
        """
        return f"{STUB_MODEL_NAME_PREFIX}{self._model_name}"

    # ------------------------------------------------------------------
    # Lifecycle — SDS §5.6 context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> StubEmbedder:
        """Enter the embedding pass. Loads nothing; flips the loaded flag."""
        self._loaded = True
        logger.info("embedder_loaded", backend_id=self.backend_id, model_name=self._model_name)
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: object,
    ) -> None:
        """Leave the embedding pass."""
        self._loaded = False
        logger.info("embedder_unloaded", backend_id=self.backend_id)

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return one deterministic unit vector per input text.

        Args:
            texts: Texts to embed. An empty list returns an empty
                   ``(0, embedding_dim)`` array rather than raising.

        Returns:
            float32 array of shape ``(len(texts), embedding_dim)``, each row a
            unit vector.

        Raises:
            RuntimeError: If called outside the context manager, matching the
                          contract the real backend must honour (§5.6).
        """
        import numpy as np  # noqa: PLC0415 - optional dependency, see module docstring

        if not self._loaded:
            raise RuntimeError(
                "StubEmbedder.embed() called outside its context manager. "
                "Use `with embedder:` before embedding (SDS §5.6)."
            )

        if not texts:
            return np.empty((0, STUB_EMBEDDING_DIM), dtype=np.float32)

        rows = [self._vector_for(text) for text in texts]
        return np.stack(rows).astype(np.float32)

    def _vector_for(self, text: str) -> np.ndarray:
        """Derive one unit vector from the SHA-256 of ``text``.

        The digest seeds a NumPy generator, so the vector is stable across
        processes and platforms — unlike ``hash()``, which is salted per run.
        """
        import numpy as np  # noqa: PLC0415 - optional dependency, see module docstring

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "big")
        generator = np.random.default_rng(seed)

        vector = generator.standard_normal(STUB_EMBEDDING_DIM)
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:  # pragma: no cover - unreachable for a Gaussian draw
            vector[0] = 1.0
            norm = 1.0
        return vector / norm
