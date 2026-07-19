"""
Pipeline orchestrator — Phase 0 stub.

In Phase 1, this class drives the full stage sequence:
    collect → rank → embed → generate → review → publish → archive

In Phase 0, it exists only to give the CLI entrypoint something to import
and to document the interface that Phase 1 will implement.
"""

from __future__ import annotations


class PipelineOrchestrator:
    """Drives one complete pipeline run from collection to archiving.

    Phase 0 stub — not yet implemented.
    Phase 1 will implement run_once() with all stages wired up.
    """

    def run_once(self) -> None:
        """Execute one complete pipeline run.

        Raises:
            NotImplementedError: Until Phase 1 is implemented.
        """
        raise NotImplementedError(
            "PipelineOrchestrator.run_once() is implemented in Phase 1."
        )
