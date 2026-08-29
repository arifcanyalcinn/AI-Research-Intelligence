"""
RankStage — score collected items and apply the importance threshold.

The second stage of the pipeline (SDS §6, §8.2). It loads every item sitting in
``COLLECTED``, scores the batch with :class:`~arip.ranking.scorer.Scorer`, and
transitions each item to ``RANKED`` or ``FILTERED`` according to
``config.ranking.min_score``.

Ownership of the ``COLLECTED`` fan-out is split between two stages (SDS §8.5):

  - ``CollectStage`` normalizes and owns ``COLLECTED -> FAILED``.
  - ``RankStage`` scores and owns ``COLLECTED -> RANKED`` and
    ``COLLECTED -> FILTERED``.

Scoring is batch-scoped because the engagement signal is a percentile within
the current run (§5.5). Every ``COLLECTED`` item forms one batch — including
items collected on an earlier run that have not yet been ranked.

What this stage does not do:

  - It applies no item cap. ``pipeline.max_items_per_run`` has no defined
    enforcement point and is deferred to Phase 3 (§9.2).
  - It writes no ``filtered_reason``. That column does not exist, and §3.5
    narrows the ``COLLECTED -> FILTERED`` action to ``importance_score`` alone.
  - It computes nothing itself. All arithmetic lives in ``Scorer``; this stage
    selects, decides and persists.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from arip.db.repositories.items import ItemRepository
from arip.enums import ItemStatus
from arip.ranking.scorer import Scorer
from arip.state_machine import StateMachine

if TYPE_CHECKING:
    from arip.db.models import Item

logger = structlog.get_logger(__name__)


class RankStage:
    """Scores ``COLLECTED`` items and applies the ``min_score`` threshold.

    All dependencies are injected (AD-11). The repository and state machine
    must share the session the orchestrator opened for this stage — this class
    never creates, commits, or closes a session.
    """

    def __init__(
        self,
        item_repo: ItemRepository,
        scorer: Scorer,
        state_machine: StateMachine,
        min_score: float,
    ) -> None:
        """
        Args:
            item_repo: Persistence for the ``items`` table.
            scorer: Computes the composite score and per-signal breakdown.
            state_machine: Sole permitted mutator of ``item.status`` (AD-03).
            min_score: Threshold from ``config.ranking.min_score``. An item
                       scoring at or above it is RANKED; below it, FILTERED.
        """
        self._item_repo = item_repo
        self._scorer = scorer
        self._state_machine = state_machine
        self._min_score = min_score

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self, run_id: int) -> None:
        """Score and dispose of every item currently in ``COLLECTED``.

        Args:
            run_id: Identifier of the current ``pipeline_runs`` row, bound into
                    the logging context for every line this stage emits.
        """
        items = self._item_repo.get_by_status(ItemStatus.COLLECTED)

        logger.info(
            "rank_stage_started",
            run_id=run_id,
            stage="ranking",
            item_count=len(items),
            min_score=self._min_score,
        )

        if not items:
            logger.info(
                "rank_stage_completed",
                run_id=run_id,
                stage="ranking",
                ranked=0,
                filtered=0,
            )
            return

        results = self._scorer.score_batch(items)

        ranked = 0
        filtered = 0
        for item, (score, breakdown) in zip(items, results, strict=True):
            with structlog.contextvars.bound_contextvars(
                run_id=run_id,
                stage="ranking",
                item_id=item.id,
                source_id=item.source_id,
            ):
                if score >= self._min_score:
                    self._promote(item, score, breakdown)
                    ranked += 1
                else:
                    self._filter(item, score, breakdown)
                    filtered += 1

        logger.info(
            "rank_stage_completed",
            run_id=run_id,
            stage="ranking",
            ranked=ranked,
            filtered=filtered,
        )

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def _promote(self, item: Item, score: float, breakdown: dict[str, float]) -> None:
        """``COLLECTED -> RANKED``: score at or above the threshold.

        Per §3.3 this sets ``importance_score`` and ``signal_breakdown``.
        ``ranked_at`` is stamped automatically by the state machine.
        """
        self._state_machine.transition(
            item,
            ItemStatus.RANKED,
            context={
                "importance_score": score,
                "signal_breakdown": json.dumps(breakdown),
            },
        )
        logger.info("item_ranked", importance_score=score, **breakdown)

    def _filter(self, item: Item, score: float, breakdown: dict[str, float]) -> None:
        """``COLLECTED -> FILTERED``: score below the threshold.

        Per §3.5 this sets ``importance_score`` only — ``filtered_reason`` is
        not a column, and ``FILTERED`` has exactly one cause in the state
        machine, so the score alone records the decision. The breakdown is
        logged rather than stored, so the reason a specific item fell short is
        still recoverable from the run log.
        """
        self._state_machine.transition(
            item,
            ItemStatus.FILTERED,
            context={"importance_score": score},
        )
        logger.info("item_filtered", importance_score=score, **breakdown)
