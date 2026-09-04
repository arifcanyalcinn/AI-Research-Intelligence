"""
Pipeline orchestrator — drives one complete pipeline run.

Stage sequence as of Batch 8 (SDS §8.2):

    reconcile crashed runs → open run → log source health → collect → rank
    → close run

The remaining stages — embed, generate, review, publish, archive — are
delivered in subsequent batches and are wired in here as they arrive.

Session boundaries
------------------

SDS §5.14 wraps each pipeline stage in a single ``session_scope()``, which
commits on clean exit and rolls back on any exception. The run bookkeeping is
therefore kept in its own scopes, separate from the stage:

  1. reconcile + open the run       → committed immediately
  2. the stage                      → committed, or rolled back on failure
  3. close the run COMPLETED/FAILED → committed

Sharing one scope across all three would roll the run row back together with
the stage's work, losing the very record that says the run failed. Splitting
them also means a hard process kill leaves the row in RUNNING, which is
exactly the state the §4.6 startup guard exists to reconcile.
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session, sessionmaker

from arip.db.database import session_scope
from arip.db.repositories.items import ItemRepository
from arip.db.repositories.pipeline_runs import PipelineRunRepository
from arip.db.repositories.raw_payloads import RawPayloadRepository
from arip.pipeline.stages.collect import CollectStage
from arip.pipeline.stages.rank import RankStage
from arip.ranking.scorer import Scorer
from arip.sources.registry import SourceRegistry
from arip.state_machine import StateMachine

logger = structlog.get_logger(__name__)


class PipelineOrchestrator:
    """Drives one complete pipeline run.

    Constructed once at startup by ``arip.container`` and given its
    dependencies explicitly (AD-11). Holds no state between runs.
    """

    def __init__(
        self,
        registry: SourceRegistry,
        session_factory: sessionmaker[Session],
        scorer: Scorer,
        min_score: float,
    ) -> None:
        """
        Args:
            registry: Supplies the active source plugins.
            session_factory: Produces the sessions each stage runs in.
            scorer: Computes item scores for the ranking stage.
            min_score: Ranking threshold from ``config.ranking.min_score``.

        The orchestrator receives finished collaborators and single values,
        never a settings object. ``SourceRegistry`` and ``Scorer`` are both
        built from configuration in ``container.py`` and injected here already
        constructed — the pattern Batch 7 established for the registry.
        """
        self._registry = registry
        self._session_factory = session_factory
        self._scorer = scorer
        self._min_score = min_score

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_once(self) -> int:
        """Execute one complete pipeline run.

        Returns:
            The ``pipeline_runs.id`` of the run just executed.

        Raises:
            Exception: Whatever a stage raised, re-raised after the run has
                       been recorded as FAILED. The caller decides the exit
                       code; the database already records what happened.
        """
        run_id = self._open_run()

        self._log_source_health(run_id)

        try:
            self._collect(run_id)
            self._rank(run_id)
        except Exception as exc:
            with session_scope(self._session_factory) as session:
                PipelineRunRepository(session).fail(run_id, str(exc))
            logger.error(
                "pipeline_run_aborted",
                run_id=run_id,
                error=str(exc),
                exc_info=True,
            )
            raise

        with session_scope(self._session_factory) as session:
            PipelineRunRepository(session).complete(run_id)

        logger.info("pipeline_run_finished", run_id=run_id)
        return run_id

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def _open_run(self) -> int:
        """Reconcile any crashed run, then open a new one.

        SDS §4.6: a row still marked RUNNING at startup is the residue of a
        process that died without closing it. It is reconciled to FAILED
        before the new run opens.

        Returns:
            The id of the newly opened run.
        """
        with session_scope(self._session_factory) as session:
            run_repo = PipelineRunRepository(session)

            reconciled = run_repo.reconcile_crashed()
            if reconciled:
                logger.warning("crashed_runs_reconciled", count=reconciled)

            run = run_repo.create()
            return run.id

    # ------------------------------------------------------------------
    # Source health — SDS §8, Phase 2 deliverable
    # ------------------------------------------------------------------

    def _log_source_health(self, run_id: int) -> None:
        """Log a SourceHealth line for every active source.

        ``health_check()`` is best-effort by contract (§5.3) and its default
        implementation cannot fail, but a plugin override performs network
        I/O. Failing to report health must never prevent collection, so any
        exception is logged and that source is simply skipped here.
        """
        for source in self._registry.get_active_sources():
            with structlog.contextvars.bound_contextvars(
                run_id=run_id,
                stage="health_check",
                source_id=source.source_id,
            ):
                try:
                    health = source.health_check()
                except Exception as exc:
                    logger.error("source_health_check_failed", error=str(exc))
                    continue

                if health.is_healthy:
                    logger.info("source_health", is_healthy=True)
                else:
                    logger.warning(
                        "source_health",
                        is_healthy=False,
                        last_error=health.last_error,
                    )

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _collect(self, run_id: int) -> None:
        """Run the collection stage inside its own session scope."""
        with session_scope(self._session_factory) as session:
            item_repo = ItemRepository(session)
            stage = CollectStage(
                registry=self._registry,
                item_repo=item_repo,
                payload_repo=RawPayloadRepository(session),
                state_machine=StateMachine(item_repo),
            )
            stage.run(run_id)

    def _rank(self, run_id: int) -> None:
        """Run the ranking stage inside its own session scope.

        A separate scope from collection, per §5.14: each stage is one unit of
        work. Ranking reads whatever collection committed, so items collected
        by the preceding stage are scored in the same run.
        """
        with session_scope(self._session_factory) as session:
            item_repo = ItemRepository(session)
            stage = RankStage(
                item_repo=item_repo,
                scorer=self._scorer,
                state_machine=StateMachine(item_repo),
                min_score=self._min_score,
            )
            stage.run(run_id)
