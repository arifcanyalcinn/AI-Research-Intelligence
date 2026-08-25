"""
PipelineRunRepository — all database access for the pipeline_runs table.

No base class. Plain Python class with exactly the methods its callers need
(SDS §5.14.1, AD-10). Testing: pass a Session connected to an in-memory SQLite
database.

Every method operates within the caller's session. The caller is responsible
for committing or rolling back. PipelineRunRepository never commits or closes
the session.

This table backs the concurrency guard specified in SDS §4.6: on startup, any
run still marked RUNNING is the residue of a crashed process and is reconciled
to FAILED before a new run opens.

Not implemented here, deliberately:

  - ``stage_metrics`` is a Phase 7 deliverable (SDS §8, Phase 7). The column
    stays NULL until then, so ``complete()`` takes no metrics argument.
  - The schedule-time defensive check in §4.6 belongs to the Scheduler (§5.1),
    which is out of Batch 7 scope. Only the startup path is provided.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from arip.db.models import PipelineRun
from arip.enums import PipelineRunStatus

logger = structlog.get_logger(__name__)


class PipelineRunRepository:
    """Data access object for the pipeline_runs table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create(self) -> PipelineRun:
        """Open a new pipeline run with status RUNNING.

        Returns:
            The newly created PipelineRun with its auto-assigned id populated
            and ``started_at`` set to the current UTC time.
        """
        run = PipelineRun(
            started_at=datetime.utcnow(),
            status=PipelineRunStatus.RUNNING.value,
        )
        self._session.add(run)
        self._session.flush()  # Populate run.id without committing.
        logger.info("pipeline_run_started", run_id=run.id)
        return run

    def complete(self, run_id: int) -> None:
        """Mark a run COMPLETED and stamp ``completed_at``.

        Args:
            run_id: Primary key of the run to close.
        """
        run = self._session.get(PipelineRun, run_id)
        if run is None:
            logger.error("pipeline_run_not_found", run_id=run_id, operation="complete")
            return

        run.status = PipelineRunStatus.COMPLETED.value
        run.completed_at = datetime.utcnow()
        self._session.flush()
        logger.info("pipeline_run_completed", run_id=run_id)

    def fail(self, run_id: int, error_summary: str) -> None:
        """Mark a run FAILED, record why, and stamp ``completed_at``.

        ``completed_at`` is set because the run did finish — unsuccessfully,
        but at a time this process observed. Contrast ``reconcile_crashed()``,
        which cannot know when the previous process died and therefore leaves
        the column NULL.

        Args:
            run_id: Primary key of the run to fail.
            error_summary: Human-readable description of what went wrong,
                           stored in ``pipeline_runs.error_summary``.
        """
        run = self._session.get(PipelineRun, run_id)
        if run is None:
            logger.error("pipeline_run_not_found", run_id=run_id, operation="fail")
            return

        run.status = PipelineRunStatus.FAILED.value
        run.completed_at = datetime.utcnow()
        run.error_summary = error_summary
        self._session.flush()
        logger.error("pipeline_run_failed", run_id=run_id, error_summary=error_summary)

    def reconcile_crashed(self) -> int:
        """Mark every run still in RUNNING as FAILED. SDS §4.6 startup guard.

        A run left in RUNNING means the previous process died without closing
        it. ``completed_at`` is deliberately left NULL: the time of death is
        unknown, and writing the current time would record a value that never
        happened.

        Call once at startup, before opening a new run.

        Returns:
            The number of runs reconciled. Zero on a clean previous shutdown.
        """
        stmt = select(PipelineRun).where(
            PipelineRun.status == PipelineRunStatus.RUNNING.value
        )
        stale = list(self._session.execute(stmt).scalars().all())

        for run in stale:
            run.status = PipelineRunStatus.FAILED.value
            run.error_summary = (
                "Run was still RUNNING at startup; the previous process "
                "terminated without closing it."
            )
            logger.warning("pipeline_run_reconciled", run_id=run.id)

        if stale:
            self._session.flush()

        return len(stale)
