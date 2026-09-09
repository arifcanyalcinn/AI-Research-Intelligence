"""
SemanticDeduplicator — ANN similarity search over a persisted index (SDS §5.7).

The four methods §5.7 names, and what each is responsible for::

    load_index()                  # loads from disk or creates new
    save_index()                  # persists after additions
    is_duplicate(vector, item_id) # -> (is_dup, nearest_neighbour_id)
    add(item_id, vector)          # index the vector

**Check first, add after.** §3.3 places the vector's addition at the
``RANKED -> EMBEDDED`` transition; §5.7 says embeddings "are added *after* the
dedup check". §5.7 governs. Adding before checking would make every item its own
nearest neighbour at similarity 1.0, and the pipeline would mark 100% of items
DUPLICATE. ``is_duplicate()`` and ``add()`` are kept as two calls, never fused
into a ``check_and_add()``, so that ordering stays visible at the call site.

**Self-exclusion.** ``is_duplicate()`` ignores any hit whose key equals
``item_id``. Under correct ordering an item is never in the index when it is
checked, so this never fires in a clean run. It exists for the run *after* a
crash between the database commit and ``save_index()``: reconciliation re-adds
the item, and without this guard the item would then match itself at 1.0 and be
marked a duplicate of itself. That is why ``top_k`` matters — the search asks
for ``top_k`` neighbours so that excluding a self-hit still leaves a real one.

**Order of writes.** The caller commits the database before calling
``save_index()``. The reverse is unsafe: an index containing an item the
database never recorded is unreconcilable, because §5.7's reconciliation query
walks *from* the database *to* the index and can only add what it finds. An
item in the database but missing from the index is the recoverable direction,
and is exactly what reconciliation repairs.

**Optional dependency.** ``usearch`` and ``numpy`` arrive with the
``embedding`` extra and are imported inside functions, never at module level —
the same discipline the embedding backends follow, for the same reason.

**A measured correction to §5.7's rationale.** §5.7 justifies ``usearch`` as
"pure Python, no native BLAS dependency issues on Windows". The installed
wheel is not pure Python: usearch 2.26.2 ships compiled extension modules. The
*conclusion* stands — it installs cleanly on Windows from a prebuilt wheel and
needs no BLAS — but the stated reason is wrong, and no code here relies on it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import numpy as np
    from usearch.index import Index

    from arip.config import DedupSettings

logger = structlog.get_logger(__name__)

INDEX_METRIC = "cos"
"""Similarity metric for the ANN index.

Cosine, because §5.7 compares embeddings by cosine similarity and §5.6's
testing strategy speaks of unit vectors. usearch reports cosine *distance*, so
``similarity = 1.0 - distance`` throughout this module — verified against
directly-computed dot products to six decimal places.

Not configurable: changing the metric would silently invalidate every vector in
an existing index file, and ``semantic_threshold`` is expressed in cosine
similarity by §5.7.
"""

_TEMP_SUFFIX = ".tmp"


class SemanticDeduplicator:
    """Near-duplicate detection against a persisted ``usearch`` ANN index.

    Not a context manager. The index is an in-memory structure with an explicit
    ``load_index()`` / ``save_index()`` lifecycle that §5.7 specifies directly,
    and it must survive the embedder's ``with`` block — the caller loads once at
    the start of the pass and saves once at the end, after committing.
    """

    def __init__(self, config: DedupSettings, embedding_dim: int) -> None:
        """
        Args:
            config: The ``dedup`` block of ``AppSettings`` — ``semantic_threshold``,
                    ``ann_index_path`` and ``top_k``.
            embedding_dim: Vector width the index is built for. Required rather
                    than inferred: §5.7 says ``load_index()`` "creates new" when
                    no file exists, and an index cannot be created without a
                    width. It is also the only way to detect a file written at a
                    different width — see ``load_index()``.
        """
        self._threshold = config.semantic_threshold
        self._top_k = config.top_k
        self._index_path = Path(config.ann_index_path)
        self._embedding_dim = embedding_dim
        self._index: Index | None = None

    # ------------------------------------------------------------------
    # Lifecycle — SDS §5.7
    # ------------------------------------------------------------------

    def load_index(self) -> None:
        """Load the index from disk, or create an empty one.

        Four outcomes, all of which leave a usable index in place:

        - **No file.** Normal first run. An empty index is created.
        - **A readable file of the right width.** Loaded as-is.
        - **A corrupt file.** §5.7: "delete file and rebuild from scratch". The
          file is deleted and an empty index created; refilling it is
          reconciliation's job, which the caller runs next.
        - **A readable file of the wrong width.** Treated as corrupt, for the
          reason in the code comment below.

        A failure to read is never fatal. The index is a derived artefact — the
        database is the record — so losing it costs a re-embed, not data.
        """
        if not self._index_path.exists():
            self._index = self._new_index()
            logger.info(
                "ann_index_created",
                path=str(self._index_path),
                embedding_dim=self._embedding_dim,
            )
            return

        index = self._new_index()
        try:
            index.load(str(self._index_path))
        except Exception as exc:
            logger.warning(
                "ann_index_corrupt_rebuilding",
                path=str(self._index_path),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            self._discard_index_file()
            self._index = self._new_index()
            return

        # usearch adopts the width recorded in the file, silently overwriting
        # the ndim this index was constructed with. Measured on 2.26.2: loading
        # a 384-wide file into an Index(ndim=768) yields ndim 768 -> 384 with no
        # error. Every later add() and search() at the configured width would
        # then raise ValueError, and §5.7's malformed-vector rule would swallow
        # each one individually — the pipeline would run, find no duplicates
        # ever, and say nothing. Failing over to a rebuild here turns a silent
        # permanent fault into one warning and one expensive run.
        if index.ndim != self._embedding_dim:
            logger.warning(
                "ann_index_dimension_mismatch_rebuilding",
                path=str(self._index_path),
                file_dim=int(index.ndim),
                expected_dim=self._embedding_dim,
            )
            self._discard_index_file()
            self._index = self._new_index()
            return

        self._index = index
        logger.info(
            "ann_index_loaded",
            path=str(self._index_path),
            size=len(index),
            embedding_dim=self._embedding_dim,
        )

    def save_index(self) -> None:
        """Persist the index.

        Written to a sibling temporary file and moved into place with
        ``os.replace``, which is atomic on POSIX and on Windows. Without that, a
        crash mid-write leaves a truncated file that ``load_index()`` can only
        treat as corrupt — costing a full re-embed of every eligible item. The
        atomic move means an interrupted save leaves the *previous* index
        intact, and the run's additions are recovered by reconciliation.

        Raises:
            RuntimeError: If called before ``load_index()``.
        """
        index = self._require_index()

        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._index_path.with_name(self._index_path.name + _TEMP_SUFFIX)

        index.save(str(temp_path))
        os.replace(temp_path, self._index_path)

        logger.info(
            "ann_index_saved",
            path=str(self._index_path),
            size=len(index),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_duplicate(self, vector: np.ndarray, item_id: int) -> tuple[bool, int | None]:
        """Decide whether ``vector`` duplicates something already indexed.

        Args:
            vector: The item's embedding, ``(embedding_dim,)``.
            item_id: The item being checked. Any hit with this key is skipped —
                     see the module docstring on self-exclusion.

        Returns:
            ``(is_duplicate, nearest_neighbour_id)``.

            The second element is the nearest **other** item whenever one
            exists, whether or not it crossed the threshold — the SDS names it
            ``nearest_neighbor_id``, not ``duplicate_of_id``, and a near miss is
            worth having in the caller's log. It is ``None`` only when the index
            holds no other item, or when the search was skipped because the
            vector was malformed (§5.7: "log, skip semantic check, continue with
            exact check only"), in which case the first element is ``False``.

        Raises:
            RuntimeError: If called before ``load_index()``.
        """
        neighbour_id, similarity = self.nearest(vector, item_id)

        if neighbour_id is None:
            return (False, None)

        is_dup = similarity >= self._threshold
        logger.info(
            "semantic_dedup_decision",
            item_id=item_id,
            nearest_item_id=neighbour_id,
            similarity=round(similarity, 6),
            threshold=self._threshold,
            is_duplicate=is_dup,
        )
        return (is_dup, neighbour_id)

    def contains(self, item_id: int) -> bool:
        """Whether ``item_id`` is already in the index.

        Required by §5.7's reconciliation, which says in as many words: "check
        if ``item_id`` is in the loaded index. If not, re-embed and add." This
        is a read-only query, not a fifth behaviour — in particular it is not
        the ``check_and_add()`` that was ruled out, which would have hidden the
        check-then-add ordering the module docstring turns on.

        Raises:
            RuntimeError: If called before ``load_index()``.
        """
        return bool(self._require_index().contains(item_id))

    @property
    def size(self) -> int:
        """Number of vectors in the index.

        Raises:
            RuntimeError: If called before ``load_index()``.
        """
        return len(self._require_index())

    @property
    def threshold(self) -> float:
        """Cosine similarity at or above which an item is a duplicate."""
        return self._threshold

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, item_id: int, vector: np.ndarray) -> None:
        """Add a vector to the index, keyed by item id.

        Call this only *after* ``is_duplicate()`` has cleared the item — see the
        module docstring.

        Neither an already-present key nor a malformed vector raises. usearch
        rejects a duplicate key outright, and reconciliation legitimately
        revisits items that are already indexed; §5.7 requires a malformed
        vector to be logged and skipped rather than to end the run.

        Raises:
            RuntimeError: If called before ``load_index()``.
        """
        index = self._require_index()

        if index.contains(item_id):
            logger.debug("ann_index_add_skipped_present", item_id=item_id)
            return

        try:
            index.add(item_id, vector)
        except Exception as exc:
            logger.warning(
                "ann_index_add_skipped_malformed_vector",
                item_id=item_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        logger.debug("ann_index_item_added", item_id=item_id, size=len(index))

    # ------------------------------------------------------------------
    # Search primitive — shared by is_duplicate() and the novelty signal
    # ------------------------------------------------------------------

    def nearest(self, vector: np.ndarray, item_id: int) -> tuple[int | None, float]:
        """Nearest indexed item other than ``item_id``, and its cosine similarity.

        Public because ``EmbedStage`` needs the *similarity* as well as the
        verdict: §5.5 stores the ANN distance to the nearest neighbour in
        ``items.signal_breakdown`` as the novelty signal (AD-19 — recorded, not
        scored), and §5.7 froze ``is_duplicate()`` to return
        ``(bool, int | None)`` with no room for it. This method is the accessor
        for the value; ``is_duplicate()`` is the accessor for the decision, and
        remains the only place the threshold comparison happens.

        Returns ``(None, 0.0)`` when there is no such item, or when the search
        could not run. ``top_k`` is the search ``k`` and only the closest
        surviving hit is used: the extra hits exist so that discarding a
        self-match still leaves a genuine neighbour, not so that several
        candidates are considered.

        Raises:
            RuntimeError: If called before ``load_index()``.
        """
        index = self._require_index()

        if len(index) == 0:
            return (None, 0.0)

        try:
            matches = index.search(vector, self._top_k)
        except Exception as exc:
            # §5.7: "usearch raises on malformed vector: log, skip semantic
            # check, continue with exact check only."
            logger.warning(
                "semantic_dedup_skipped_malformed_vector",
                item_id=item_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return (None, 0.0)

        for key, distance in zip(matches.keys, matches.distances, strict=True):
            neighbour_id = int(key)
            if neighbour_id == item_id:
                continue
            return (neighbour_id, 1.0 - float(distance))

        return (None, 0.0)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _new_index(self) -> Index:
        """An empty index at the configured width and metric."""
        from usearch.index import Index  # noqa: PLC0415 - optional dependency

        return Index(ndim=self._embedding_dim, metric=INDEX_METRIC, dtype="f32")

    def _discard_index_file(self) -> None:
        """Remove an unusable index file, tolerating its absence."""
        try:
            self._index_path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem-dependent
            logger.warning(
                "ann_index_delete_failed",
                path=str(self._index_path),
                error=str(exc),
            )

    def _require_index(self) -> Index:
        """The loaded index, or a clear error.

        Every public method needs it. Returning ``(False, None)`` from an
        unloaded deduplicator would look like "no duplicates found" and let a
        whole run pass without deduplication.
        """
        if self._index is None:
            raise RuntimeError(
                "SemanticDeduplicator used before load_index(). Call "
                "load_index() once at the start of the embedding pass (SDS §5.7)."
            )
        return self._index
