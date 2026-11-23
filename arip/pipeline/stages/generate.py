"""
GenerateStage — select ENRICHED items, generate their content, and transition them.

The fourth stage of the pipeline (SDS §5.10.2, §9.2.1, §3.3, D-017). It owns the
two transitions reachable from generation:

    ENRICHED -> GENERATED   gen_ok
    ENRICHED -> FAILED      gen_max_retry, template error, LLMError

One pass
--------

    1. Selection, in one session, for every language of ``pipeline.languages``
       in the configured order: the ENRICHED items in D-010 order
       (``ItemRepository.get_by_status_and_language()``); an item whose
       ``abstract`` is NULL or empty after ``str.strip()`` is skipped; the first
       ``max_items_per_run`` eligible items are selected (§9.2.1, cap per
       language). Nothing selected anywhere: no model is loaded.
    2. One ``with llm_factory() as llm:`` block for the whole pass (§5.9, AD-05).
    3. Each selected item in its own ``session_scope()``: ``generate(item)``
       inside the per-item logging context (§5.16), then the transition. The
       scope commits before the next item begins.
    4. One ``generation_stage_completed`` line per language, also when the pass
       stops.

The ``LLMError`` path (D-015 requirement 1)
-------------------------------------------

An ``LLMError`` is caught inside the item's scope. The item is marked FAILED,
and the error is *returned* out of the ``with`` block, so the scope exits
normally and commits the FAILED transition together with the attempt rows the
generator stored before the error. Only then is the error raised, which stops
the pass (threshold 1, §5.10.2). Raising it inside the scope would roll all of
that back.

Any other exception (including a ``ConfigError``) is not caught: the item's
scope rolls back, the item stays ENRICHED, and the pass stops.

A FAILED item has no active row (§5.10.2): ``deactivate_all_for_item()`` is
called whenever this stage marks an item FAILED.

What this stage does not do
---------------------------

  - It receives no embedder. The orchestrator runs it only after the embedding
    stage has returned (AD-06, §7.1).
  - It does not write ``pipeline_runs.stage_metrics`` (Phase 7, §8.4).
  - It applies no cap other than §9.2.1's; collection, ranking and embedding
    apply none.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from arip.db.database import session_scope
from arip.db.repositories.content import ContentRepository
from arip.db.repositories.items import ItemRepository
from arip.enums import ItemStatus
from arip.exceptions import LLMError
from arip.generation.generator import ContentGenerator
from arip.state_machine import StateMachine

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.orm import Session, sessionmaker

    from arip.config import GenerationParams
    from arip.db.models import Item
    from arip.enums import Language
    from arip.generation.prompt_manager import PromptManager
    from arip.generation.validator import ContentValidator
    from arip.interfaces import BaseLLM

logger = structlog.get_logger(__name__)

STAGE = "generation"
FAILED_AT_STAGE = "GENERATION"
"""``items.failed_at_stage`` for every failure of this stage (§3.3, ruling Q2 of Stage 7)."""


# ---------------------------------------------------------------------------
# Selection (§9.2.1)
# ---------------------------------------------------------------------------


def is_eligible(item: Item) -> bool:
    """§9.2.1: the source text (``abstract``) is not NULL and not empty after ``str.strip()``."""
    return item.abstract is not None and item.abstract.strip() != ""


@dataclass
class Selection:
    """The result of selecting one language's ENRICHED items (§9.2.1)."""

    enriched: int
    """ENRICHED items found for the language."""
    ineligible: int
    """Of those, items skipped for empty source text."""
    selected: list[int]
    """Ids of the selected items, in D-010 order."""
    beyond_cap: int
    """Eligible items not selected because the cap was reached."""


def select_for_generation(items: Sequence[Item], cap: int) -> Selection:
    """Apply §9.2.1 to ``items``, which must already be in D-010 order.

    Ineligible items are skipped and do not count against ``cap``; the first
    ``cap`` eligible items are selected; the rest are counted as beyond the cap.
    """
    selected: list[int] = []
    ineligible = beyond_cap = 0
    for item in items:
        if not is_eligible(item):
            ineligible += 1
        elif len(selected) < cap:
            selected.append(item.id)
        else:
            beyond_cap += 1
    return Selection(
        enriched=len(items), ineligible=ineligible, selected=selected, beyond_cap=beyond_cap
    )


@dataclass
class _LanguageCounts:
    language: Language
    selection: Selection
    generated: int = 0
    failed: int = 0

    @property
    def not_processed(self) -> int:
        return len(self.selection.selected) - self.generated - self.failed


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


class GenerateStage:
    """Generates content for selected ENRICHED items (SDS §5.10.2).

    All dependencies are injected (AD-11). Unlike the other stages it receives
    the session factory, not repositories: it opens one session for selection
    and one per item, because each item commits on its own (§5.10.2).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm_factory: Callable[[], BaseLLM],
        prompt_manager: PromptManager,
        validator: ContentValidator,
        params: GenerationParams,
        max_retries: int,
        languages: Sequence[Language],
        max_items_per_run: int,
    ) -> None:
        """
        Args:
            session_factory: Produces the selection session and the per-item sessions.
            llm_factory: Returns a fresh, unloaded backend —
                ``LLMRegistry.get_backend`` bound in ``container.py``. Entered
                once per pass, and only when an item is selected.
            prompt_manager: Templates already checked at startup (§5.15.3).
            validator: ``ContentValidator(config.generation.validation)``.
            params: ``config.llm.generation``.
            max_retries: ``config.generation.max_retries``.
            languages: ``pipeline.languages`` as ``Language`` values, in order.
            max_items_per_run: ``pipeline.max_items_per_run``, the cap per language.
        """
        self._session_factory = session_factory
        self._llm_factory = llm_factory
        self._prompt_manager = prompt_manager
        self._validator = validator
        self._params = params
        self._max_retries = max_retries
        self._languages = list(languages)
        self._max_items_per_run = max_items_per_run

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self, run_id: int) -> None:
        """Run one generation pass.

        Raises:
            LLMError: The first ``LLMError`` of the pass, after the item it
                      occurred on has been committed as FAILED; or the load
                      failure of the model, with no item changed.
            Exception: Any other exception, after the current item's scope has
                       rolled back. In every case the LLM's context manager has
                       exited first.
        """
        started = time.monotonic()
        logger.info(
            "generation_stage_started",
            run_id=run_id,
            stage=STAGE,
            languages=[language.value for language in self._languages],
            max_items_per_run=self._max_items_per_run,
        )
        counts = self._select()
        stopped = False
        current: int | None = None
        try:
            if any(entry.selection.selected for entry in counts):
                with self._llm_factory() as llm:
                    for entry in counts:
                        for item_id in entry.selection.selected:
                            current = item_id
                            error = self._process(run_id, llm, item_id, entry)
                            if error is not None:
                                raise error
        except Exception as exc:
            stopped = True
            logger.error(
                "generation_pass_stopped",
                run_id=run_id,
                stage=STAGE,
                item_id=current,
                error=str(exc),
            )
            raise
        finally:
            self._log_completed(run_id, counts, stopped, started)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _select(self) -> list[_LanguageCounts]:
        """Select every language's items in one read-only session (§9.2.1)."""
        counts: list[_LanguageCounts] = []
        with session_scope(self._session_factory) as session:
            item_repo = ItemRepository(session)
            for language in self._languages:
                items = item_repo.get_by_status_and_language(ItemStatus.ENRICHED, language.value)
                selection = select_for_generation(items, self._max_items_per_run)
                counts.append(_LanguageCounts(language=language, selection=selection))
        return counts

    # ------------------------------------------------------------------
    # One item
    # ------------------------------------------------------------------

    def _process(
        self, run_id: int, llm: BaseLLM, item_id: int, counts: _LanguageCounts
    ) -> LLMError | None:
        """Generate and transition one item in its own session scope.

        Returns:
            The ``LLMError`` to raise once the scope has committed, or None.
        """
        error: LLMError | None = None
        with session_scope(self._session_factory) as session:
            item_repo = ItemRepository(session)
            content_repo = ContentRepository(session)
            state_machine = StateMachine(item_repo)
            item = item_repo.get_by_id(item_id)
            generator = ContentGenerator(
                llm=llm,
                prompt_manager=self._prompt_manager,
                validator=self._validator,
                content_repo=content_repo,
                params=self._params,
                max_retries=self._max_retries,
            )
            with structlog.contextvars.bound_contextvars(
                run_id=run_id,
                stage=STAGE,
                item_id=item.id,
                source_id=item.source_id,
            ):
                try:
                    content = generator.generate(item)
                except LLMError as exc:
                    self._fail(item, generator.last_failure_reason, state_machine, content_repo)
                    error = exc
                    content = None
                else:
                    if content is None:
                        self._fail(item, generator.last_failure_reason, state_machine, content_repo)
                    else:
                        state_machine.transition(
                            item,
                            ItemStatus.GENERATED,
                            context={"generated_at": content.generated_at},
                        )
            generated = content is not None
        # The scope has committed: only now is the outcome counted.
        if generated:
            counts.generated += 1
        else:
            counts.failed += 1
        return error

    def _fail(
        self,
        item: Item,
        reason: str | None,
        state_machine: StateMachine,
        content_repo: ContentRepository,
    ) -> None:
        """``ENRICHED -> FAILED`` (§3.3), and no active row remains (§5.10.2)."""
        state_machine.transition(
            item,
            ItemStatus.FAILED,
            context={"failed_at_stage": FAILED_AT_STAGE, "failure_reason": reason},
        )
        content_repo.deactivate_all_for_item(item.id)
        logger.warning("item_generation_failed", failure_reason=reason)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_completed(
        self, run_id: int, counts: list[_LanguageCounts], stopped: bool, started: float
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        for entry in counts:
            logger.info(
                "generation_stage_completed",
                run_id=run_id,
                stage=STAGE,
                language=entry.language.value,
                enriched=entry.selection.enriched,
                ineligible=entry.selection.ineligible,
                selected=len(entry.selection.selected),
                beyond_cap=entry.selection.beyond_cap,
                generated=entry.generated,
                failed=entry.failed,
                not_processed=entry.not_processed,
                stopped=stopped,
                duration_ms=duration_ms,
            )
