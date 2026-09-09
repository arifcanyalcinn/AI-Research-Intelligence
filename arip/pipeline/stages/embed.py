"""
EmbedStage — embed ranked items, deduplicate them, and record novelty.

The third stage of the pipeline (SDS §6, §8.2). It owns all three transitions
reachable from the embedding pass:

    RANKED   -> EMBEDDED    embed_ok
    RANKED   -> FAILED      embed_error
    EMBEDDED -> ENRICHED    dedup_pass
    EMBEDDED -> DUPLICATE   dedup_hit

Per-item sequence, and why it is this order
-------------------------------------------

    1. vector = embedder.embed([text])
    2. neighbour, similarity = deduplicator.nearest(vector, item.id)   # novelty
    3. is_dup,   neighbour  = deduplicator.is_duplicate(vector, item.id)
    4. RANKED -> EMBEDDED, writing embedding_computed_at,
       embedding_model_name and the merged signal_breakdown
    5a. duplicate  -> EMBEDDED -> DUPLICATE, and the vector is NOT indexed
    5b. otherwise  -> deduplicator.add(...) then EMBEDDED -> ENRICHED

**Check before add.** §3.3 places the index update at ``RANKED -> EMBEDDED``;
§5.7 says embeddings "are added after the dedup check". §5.7 governs — adding
first would make every item its own nearest neighbour at similarity 1.0 and
mark the entire run DUPLICATE, which is terminal.

**Duplicates are never indexed.** §5.7's reconciliation query excludes
``DUPLICATE``, so an indexed duplicate could never be re-added after an index
loss, and the two stores would disagree permanently in the direction
reconciliation cannot repair.

**signal_breakdown is read, merged and written — never replaced.** ``RankStage``
wrote the four ranking signals into that column at ``COLLECTED -> RANKED``.
Serialising a fresh ``{"novelty": ...}`` object here would silently destroy
them: no exception, no constraint violation, and every ranking explanation in
the database gone. ``_merged_breakdown()`` is the only writer, and
``test_novelty_merge_preserves_the_ranking_signals`` fails if it becomes an
overwrite.

**Novelty is recorded, not scored.** AD-19 and §5.5: "the ANN distance to
nearest neighbor is stored in ``items.signal_breakdown`` but is not used in the
ranking score". ``importance_score`` is not touched here.

**Two searches per item, deliberately.** ``is_duplicate()``'s return type is
frozen by §5.7 at ``(bool, int | None)`` and carries no similarity, but the
novelty signal is that similarity. Rather than widen a frozen signature or
duplicate the threshold comparison outside the deduplicator, this stage asks
twice: ``nearest()`` for the value, ``is_duplicate()`` for the verdict. The
index is unchanged between the two calls, so they cannot disagree, and an ANN
query over a few thousand 384-dimensional vectors is microseconds.

What this stage does not do
---------------------------

  - It does not create, commit or close a session (§5.14). The orchestrator
    opens one scope for this stage and commits on clean exit.
  - It does not call ``save_index()``. The database must be committed first —
    an index entry for an item the database never recorded is the one
    divergence §5.7's reconciliation cannot repair — and the commit happens
    when the orchestrator's scope exits, after ``run()`` has returned. ``run()``
    therefore returns the deduplicator for the orchestrator to persist.
  - It applies no item cap. ``pipeline.max_items_per_run`` still has no defined
    enforcement point (§9.2).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from arip.enums import ItemStatus
from arip.exceptions import EmbeddingError

if TYPE_CHECKING:
    from collections.abc import Callable

    from arip.db.models import Item
    from arip.db.repositories.items import ItemRepository
    from arip.dedup.semantic import SemanticDeduplicator
    from arip.interfaces import BaseEmbedder
    from arip.state_machine import StateMachine

logger = structlog.get_logger(__name__)

NOVELTY_KEY = "novelty"
"""Key under which the ANN distance is merged into ``signal_breakdown``."""

NOVELTY_WITHOUT_NEIGHBOUR = 1.0
"""Novelty recorded when the index holds no other item to compare against.

The first items of the first run have no neighbour, so their ANN distance is
undefined rather than large. 1.0 is the cosine distance of an orthogonal
vector — "unrelated to anything present" — which is the honest reading of an
empty index and keeps the column numeric for every embedded row.

Chosen, not specified: §5.5 defines novelty as "the ANN distance to nearest
neighbor" and is silent on there being no neighbour. The alternative is to omit
the key, which would leave downstream readers distinguishing "no neighbour"
from "not yet embedded" by absence.
"""


class EmbedStage:
    """Embeds ``RANKED`` items, deduplicates them, and records novelty.

    All dependencies are injected (AD-11). The repository and state machine
    share the session the orchestrator opened for this stage; this class never
    creates, commits or closes one.
    """

    def __init__(
        self,
        item_repo: ItemRepository,
        state_machine: StateMachine,
        embedder: BaseEmbedder,
        deduplicator_factory: Callable[..., SemanticDeduplicator],
    ) -> None:
        """
        Args:
            item_repo: Persistence for the ``items`` table.
            state_machine: Sole permitted mutator of ``item.status`` (AD-03).
            embedder: An unloaded backend from ``EmbeddingRegistry.get_backend()``.
                      This stage enters it, so the model is loaded only when
                      there is work, and unloaded before ``run()`` returns
                      (§5.6, AD-06).
            deduplicator_factory: Called as ``factory(embedding_dim=...)`` once
                      the model is loaded. A factory rather than an instance
                      because the ANN index width must be the width of the model
                      that produced the vectors, and the real backend only knows
                      its dimension after ``__enter__`` — which is after
                      ``container.py`` has finished wiring. The dedup
                      configuration is already bound into the factory there, so
                      this stage still never sees a settings object.
        """
        self._item_repo = item_repo
        self._state_machine = state_machine
        self._embedder = embedder
        self._deduplicator_factory = deduplicator_factory

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self, run_id: int) -> SemanticDeduplicator | None:
        """Embed and deduplicate every item currently in ``RANKED``.

        Args:
            run_id: Identifier of the current ``pipeline_runs`` row, bound into
                    the logging context for every line this stage emits.

        Returns:
            The deduplicator whose index the caller must persist **after
            committing the session**, or ``None`` when there was no work and no
            index was ever created. Returning it rather than saving it here is
            what keeps the write order correct — see the module docstring.
        """
        items = self._item_repo.get_by_status(ItemStatus.RANKED)

        logger.info(
            "embed_stage_started",
            run_id=run_id,
            stage="embedding",
            item_count=len(items),
        )

        if not items:
            # No model load, no index, nothing to save. Reconciliation is
            # skipped too: it repairs the index, and an index nothing is about
            # to consult can be repaired by the next run that has work.
            logger.info(
                "embed_stage_completed",
                run_id=run_id,
                stage="embedding",
                embedded=0,
                enriched=0,
                duplicates=0,
                failed=0,
            )
            return None

        embedded = enriched = duplicates = failed = 0

        with self._embedder as embedder:
            deduplicator = self._deduplicator_factory(
                embedding_dim=embedder.embedding_dim
            )
            deduplicator.load_index()
            self._reconcile(run_id, embedder, deduplicator)

            for item in items:
                with structlog.contextvars.bound_contextvars(
                    run_id=run_id,
                    stage="embedding",
                    item_id=item.id,
                    source_id=item.source_id,
                ):
                    outcome = self._process(item, embedder, deduplicator)

                if outcome is None:
                    failed += 1
                    continue
                embedded += 1
                if outcome:
                    duplicates += 1
                else:
                    enriched += 1

        logger.info(
            "embed_stage_completed",
            run_id=run_id,
            stage="embedding",
            embedded=embedded,
            enriched=enriched,
            duplicates=duplicates,
            failed=failed,
            index_size=deduplicator.size,
        )
        return deduplicator

    # ------------------------------------------------------------------
    # Per-item processing
    # ------------------------------------------------------------------

    def _process(
        self,
        item: Item,
        embedder: BaseEmbedder,
        deduplicator: SemanticDeduplicator,
    ) -> bool | None:
        """Embed, deduplicate and transition one item.

        Returns:
            ``True`` if the item was a duplicate, ``False`` if it passed, and
            ``None`` if embedding failed and the item went to ``FAILED``.
        """
        try:
            vectors = embedder.embed([self._embedding_text(item)])
        except EmbeddingError as exc:
            self._fail(item, exc)
            return None

        vector = vectors[0]

        # Two calls; see the module docstring. nearest() supplies the novelty
        # value, is_duplicate() owns the threshold comparison.
        _, similarity = deduplicator.nearest(vector, item.id)
        is_dup, neighbour_id = deduplicator.is_duplicate(vector, item.id)

        novelty = (
            NOVELTY_WITHOUT_NEIGHBOUR if neighbour_id is None else 1.0 - similarity
        )
        self._embed_ok(item, embedder.model_name, novelty)

        if is_dup:
            self._mark_duplicate(item, neighbour_id)
            return True

        deduplicator.add(item.id, vector)
        self._state_machine.transition(item, ItemStatus.ENRICHED)
        return False

    def _embedding_text(self, item: Item) -> str:
        """The text handed to the embedder.

        SDS §5.6 specifies it in its lifecycle example::

            embedder.embed([item.title + ". " + (item.abstract or "")])

        Reproduced exactly, with one forced deviation: ``items.title`` is
        nullable in §4, so a bare ``item.title`` would raise ``TypeError`` on a
        NULL title. ``(item.title or "")`` is the narrowest fix.

        Known consequence, not a defect introduced here: TD-003 records that
        Hugging Face Models and Spaces have ``abstract = None``, and TD-004 that
        their title is the repo id. For those two sources the embedding text is
        therefore a repo id followed by ". " — real but thin. Changing it is out
        of scope for this batch.
        """
        return (item.title or "") + ". " + (item.abstract or "")

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def _embed_ok(self, item: Item, model_name: str, novelty: float) -> None:
        """``RANKED -> EMBEDDED`` (§3.3, ``embed_ok``).

        Sets ``embedding_computed_at`` and ``embedding_model_name`` as §3.3
        requires — ``EMBEDDED`` has no entry in the state machine's automatic
        timestamp map, so the timestamp is passed explicitly — and merges the
        novelty signal into ``signal_breakdown``.
        """
        self._state_machine.transition(
            item,
            ItemStatus.EMBEDDED,
            context={
                "embedding_computed_at": datetime.utcnow(),
                "embedding_model_name": model_name,
                "signal_breakdown": self._merged_breakdown(item, novelty),
            },
        )
        logger.info("item_embedded", model_name=model_name, novelty=round(novelty, 6))

    def _mark_duplicate(self, item: Item, neighbour_id: int | None) -> None:
        """``EMBEDDED -> DUPLICATE`` (§3.3, ``dedup_hit``). Terminal.

        Logs both ids and both titles, so that a duplicate decision can be
        judged from the log alone. The similarity and threshold are on the
        deduplicator's own ``semantic_dedup_decision`` record; §5.16 binds
        ``run_id`` and ``item_id`` on both, which is what joins them.
        """
        survivor = self._item_repo.get_by_id(neighbour_id) if neighbour_id else None

        self._state_machine.transition(
            item,
            ItemStatus.DUPLICATE,
            context={
                "duplicate_of_id": neighbour_id,
                "is_semantic_duplicate": True,
            },
        )
        logger.info(
            "item_marked_duplicate",
            duplicate_item_id=item.id,
            duplicate_title=item.title,
            survivor_item_id=neighbour_id,
            survivor_title=survivor.title if survivor else None,
        )

    def _fail(self, item: Item, exc: EmbeddingError) -> None:
        """``RANKED -> FAILED`` (§3.3, ``embed_error``).

        §3.3: "Embedding errors are rare (model load failure). Do not retry
        automatically." The item stays recoverable — ``FAILED -> RANKED`` is a
        valid manual re-queue for ``failed_at_stage = 'EMBEDDING'``.
        """
        self._state_machine.transition(
            item,
            ItemStatus.FAILED,
            context={
                "failed_at_stage": "EMBEDDING",
                "failure_reason": str(exc),
            },
        )
        logger.warning("item_embedding_failed", error=str(exc))

    # ------------------------------------------------------------------
    # signal_breakdown
    # ------------------------------------------------------------------

    def _merged_breakdown(self, item: Item, novelty: float) -> str:
        """Add the novelty key to the item's existing breakdown, keeping the rest.

        The whole point of this method. ``RankStage`` stored the four ranking
        signals here; this stage adds a fifth key and must preserve the four.
        A malformed or absent value is replaced rather than allowed to abort the
        run — the column is diagnostic (§4: "Per-signal scores for debugging"),
        and losing an unparseable breakdown is strictly better than failing an
        item that embedded correctly.
        """
        existing: dict[str, object] = {}
        raw = item.signal_breakdown
        if raw:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("signal_breakdown_unparseable", item_id=item.id)
            else:
                if isinstance(parsed, dict):
                    existing = parsed
                else:
                    logger.warning(
                        "signal_breakdown_not_an_object",
                        item_id=item.id,
                        found_type=type(parsed).__name__,
                    )

        merged = dict(existing)
        merged[NOVELTY_KEY] = novelty
        return json.dumps(merged)

    # ------------------------------------------------------------------
    # Reconciliation — SDS §5.7
    # ------------------------------------------------------------------

    def _reconcile(
        self,
        run_id: int,
        embedder: BaseEmbedder,
        deduplicator: SemanticDeduplicator,
    ) -> None:
        """Re-add any embedded item the index has lost (§5.7).

        §5.7's query, used exactly as ``ItemRepository.get_all_with_embeddings()``
        already implements it: ``embedding_computed_at IS NOT NULL`` and status
        not in ``(DUPLICATE, FILTERED, FAILED)``. For each result missing from
        the index, the vector is recomputed and added — ``items`` stores no
        vectors, so it cannot be copied.

        Runs at stage start rather than at process startup (§5.7 says "at
        startup"): re-embedding needs a loaded model, and AD-06 plus §5.6's
        context manager confine the model to this pass. For a process that
        performs one run these are the same moment.

        Failures are per-item and non-fatal. An item that cannot be re-embedded
        stays missing from the index and is retried next run; aborting here
        would prevent the run from doing any new work at all.
        """
        candidates = self._item_repo.get_all_with_embeddings()
        missing = [item for item in candidates if not deduplicator.contains(item.id)]

        if not missing:
            logger.info(
                "ann_index_reconciled",
                run_id=run_id,
                stage="embedding",
                checked=len(candidates),
                restored=0,
            )
            return

        restored = 0
        for item in missing:
            try:
                vector = embedder.embed([self._embedding_text(item)])[0]
            except EmbeddingError as exc:
                logger.warning(
                    "ann_index_reconcile_item_failed",
                    run_id=run_id,
                    item_id=item.id,
                    error=str(exc),
                )
                continue
            deduplicator.add(item.id, vector)
            restored += 1

        logger.info(
            "ann_index_reconciled",
            run_id=run_id,
            stage="embedding",
            checked=len(candidates),
            restored=restored,
            still_missing=len(missing) - restored,
        )
