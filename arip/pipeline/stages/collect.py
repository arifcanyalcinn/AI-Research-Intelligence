"""
CollectStage — fetch, normalize, exact-deduplicate, persist.

The first stage of the pipeline (SDS §6, §8.2). For every active source it
calls ``fetch()``, normalizes each payload through that source's own
``normalize()`` method (AD-18), discards exact duplicates, and persists the
survivors as ``COLLECTED`` items with their raw payload alongside.

Scope boundaries, per SDS §8.2:

  - Items are left in ``COLLECTED``. The transition to RANKED or FILTERED
    belongs to the ranking stage, which is a later batch.
  - ``pipeline.max_items_per_run`` is NOT applied here. Its enforcement point
    is deferred to Phase 3 (SDS §9.2).

Failure policy (SDS §8.3.2):

  - A source whose ``fetch()`` fails contributes no payloads; the remaining
    sources still run.
  - A payload that fails ``normalize()`` produces an item marked FAILED with
    ``failed_at_stage='NORMALIZATION'``; collection continues with the
    remaining payloads.

Deduplication (SDS §8.3.1):

  - A payload whose ``content_hash`` already exists is discarded before
    insertion and logged. No row is created and no state transition occurs.
  - Exact deduplication is per-source only: ``content_hash`` includes
    ``source_id`` (§4.2), so the same work fetched from two sources yields two
    distinct hashes. Cross-source duplicates are semantic deduplication's
    responsibility in Phase 3.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import structlog

from arip.db.repositories.items import ItemRepository
from arip.db.repositories.raw_payloads import RawPayloadRepository
from arip.entities import NormalizedItem, RawSourcePayload
from arip.enums import ItemStatus
from arip.interfaces import BaseSource
from arip.sources._http import compute_content_hash
from arip.sources.registry import SourceRegistry
from arip.state_machine import StateMachine

logger = structlog.get_logger(__name__)

# Outcome keys used for the per-run count summary (SDS §5.16 INFO policy).
_COLLECTED = "collected"
_DUPLICATE = "duplicates"
_FAILED = "failed"


class CollectStage:
    """Collects raw items from every active source and persists them.

    All dependencies are injected (AD-11). The repositories and state machine
    must share the session the orchestrator opened for this stage — this class
    never creates, commits, or closes a session.
    """

    def __init__(
        self,
        registry: SourceRegistry,
        item_repo: ItemRepository,
        payload_repo: RawPayloadRepository,
        state_machine: StateMachine,
    ) -> None:
        """
        Args:
            registry: Provides the active source plugins for this run.
            item_repo: Persistence for the ``items`` table.
            payload_repo: Persistence for the ``raw_source_payloads`` table.
            state_machine: Sole permitted mutator of ``item.status`` (AD-03).
        """
        self._registry = registry
        self._item_repo = item_repo
        self._payload_repo = payload_repo
        self._state_machine = state_machine

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self, run_id: int) -> None:
        """Run collection across every active source.

        Never raises on a source or payload failure — both are contained so
        that one bad source or one malformed payload cannot end the run.

        Args:
            run_id: Identifier of the current ``pipeline_runs`` row, bound
                    into the logging context for every line this stage emits.
        """
        sources = self._registry.get_active_sources()
        totals = {_COLLECTED: 0, _DUPLICATE: 0, _FAILED: 0}

        logger.info(
            "collect_stage_started",
            run_id=run_id,
            stage="collection",
            source_count=len(sources),
        )

        for source in sources:
            with structlog.contextvars.bound_contextvars(
                run_id=run_id,
                stage="collection",
                source_id=source.source_id,
            ):
                payloads = self._fetch(source)
                for payload in payloads:
                    outcome = self._process_payload(source, payload)
                    totals[outcome] += 1

        logger.info(
            "collect_stage_completed",
            run_id=run_id,
            stage="collection",
            collected=totals[_COLLECTED],
            duplicates=totals[_DUPLICATE],
            failed=totals[_FAILED],
        )

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _fetch(self, source: BaseSource) -> list[RawSourcePayload]:
        """Fetch from one source, containing any failure.

        ``BaseSource.fetch()`` is contracted not to raise (§5.3) — it logs and
        returns an empty list. This guard exists because a plugin bug must not
        be able to end the run for every other source (§5.2, §8.3.2).

        Returns:
            The payloads fetched, or an empty list if the source failed.
        """
        try:
            payloads = source.fetch()
        except Exception as exc:
            logger.error("source_fetch_failed", error=str(exc), exc_info=True)
            return []

        logger.info("source_fetched", payload_count=len(payloads))
        return payloads

    # ------------------------------------------------------------------
    # Per-payload processing
    # ------------------------------------------------------------------

    def _process_payload(self, source: BaseSource, payload: RawSourcePayload) -> str:
        """Normalize, deduplicate, and persist a single payload.

        Returns:
            One of the module-level outcome keys, for the run count summary.
        """
        with structlog.contextvars.bound_contextvars(external_id=payload.external_id):
            try:
                normalized = source.normalize(payload)
            except Exception as exc:
                # SDS §5.4 / §3.3: the item is recorded and marked FAILED so the
                # failure is visible and re-queueable, not silently dropped.
                self._record_normalization_failure(payload, str(exc))
                return _FAILED

            if self._is_already_collected(normalized):
                return _DUPLICATE

            self._persist(normalized, payload)
            return _COLLECTED

    def _is_already_collected(self, normalized: NormalizedItem) -> bool:
        """Return True when this item must not be inserted again.

        Two checks, both backed by a UNIQUE constraint declared in §4.2:

        1. ``content_hash`` — the exact-duplicate key (§5.7).
        2. ``(source_id, external_id)`` — "prevents duplicate collection from
           same source" (§4.2). This catches the case where a source revises
           an item's title between runs, which changes ``content_hash`` while
           the item is plainly the same one.

        Checking before insertion keeps the session clean; the UNIQUE
        constraints remain the authoritative backstop.
        """
        if self._item_repo.get_by_content_hash(normalized.content_hash) is not None:
            logger.info("item_duplicate_skipped", reason="content_hash")
            return True

        existing = self._item_repo.get_by_source_and_external_id(
            normalized.source_id, normalized.external_id
        )
        if existing is not None:
            logger.info(
                "item_duplicate_skipped",
                reason="source_and_external_id",
                item_id=existing.id,
            )
            return True

        return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self, normalized: NormalizedItem, payload: RawSourcePayload) -> None:
        """Insert the item and its raw payload row.

        The item is left in ``COLLECTED`` — the default assigned by
        ``ItemRepository.create()``. No state transition happens here.

        ``normalized_at`` is stamped here rather than by the
        ``COLLECTED -> RANKED`` transition that §3.3's action line names,
        because this is the stage that normalizes (SDS §8.5).
        """
        columns = _item_columns(normalized)
        # SDS §8.5: normalization happens here, so this stage stamps its time.
        # The COLLECTED -> RANKED transition stamps ranked_at separately.
        columns["normalized_at"] = datetime.utcnow()

        item = self._item_repo.create(columns)
        self._payload_repo.create(
            {
                "item_id": item.id,
                "source_id": normalized.source_id,
                "payload": normalized.raw_payload,
                "fetched_at": payload.fetched_at,
            }
        )
        logger.info("item_collected", item_id=item.id)

    def _record_normalization_failure(
        self, payload: RawSourcePayload, error: str
    ) -> None:
        """Create an item for an unnormalizable payload and mark it FAILED.

        The payload could not be normalized, so the canonical metadata is
        unavailable. The item is created from what the raw payload itself
        carries, then transitioned via the state machine (AD-03).

        ``content_hash`` is computed with an empty title: the §4.2 formula
        needs one, and no title was extracted. The value stays deterministic
        and unique per ``(source_id, external_id)``.

        The raw payload is stored regardless, so the item can be re-normalized
        without re-fetching (§4.7).
        """
        existing = self._item_repo.get_by_source_and_external_id(
            payload.source_id, payload.external_id
        )
        if existing is not None:
            # Already recorded on an earlier run; do not insert a second row.
            logger.warning(
                "normalization_failed_already_recorded",
                item_id=existing.id,
                error=error,
            )
            return

        item = self._item_repo.create(
            {
                "source_id": payload.source_id,
                "source_type": payload.source_type.value,
                "external_id": payload.external_id,
                "content_hash": compute_content_hash(
                    source_id=payload.source_id,
                    external_id=payload.external_id,
                    title="",
                ),
            }
        )
        self._payload_repo.create(
            {
                "item_id": item.id,
                "source_id": payload.source_id,
                "payload": json.dumps(payload.raw_data),
                "fetched_at": payload.fetched_at,
            }
        )

        self._state_machine.transition(
            item,
            ItemStatus.FAILED,
            context={
                "failed_at_stage": "NORMALIZATION",
                "failure_reason": error,
                "retry_count": 1,
            },
        )
        logger.error("normalization_failed", item_id=item.id, error=error)


# ---------------------------------------------------------------------------
# NormalizedItem → items column mapping
# ---------------------------------------------------------------------------


def _item_columns(normalized: NormalizedItem) -> dict[str, object]:
    """Translate a NormalizedItem into a dict of ``items`` column values.

    Three conversions are required, because NormalizedItem is a transport
    object and does not mirror the column types:

    - ``authors``, ``institutions``, ``additional_urls``, ``topics`` and
      ``source_signals`` are Python containers, while the columns are
      TEXT holding JSON (§4.2).
    - ``published_date`` is an ISO ``YYYY-MM-DD`` string, while the column is
      DATE and accepts only ``datetime.date``.
    - ``raw_payload`` is deliberately absent: it is not a column on ``items``
      (§4.7) and belongs to ``raw_source_payloads``. See TD-011.
    """
    return {
        "source_id": normalized.source_id,
        "source_type": normalized.source_type,
        "external_id": normalized.external_id,
        "content_hash": normalized.content_hash,
        "language": normalized.language,
        "title": normalized.title,
        "primary_url": normalized.primary_url,
        "abstract": normalized.abstract,
        "authors": _to_json(normalized.authors),
        "institutions": _to_json(normalized.institutions),
        "additional_urls": _to_json(normalized.additional_urls),
        "topics": _to_json(normalized.topics),
        "source_signals": _to_json(normalized.source_signals),
        "published_date": _to_date(normalized.published_date),
    }


def _to_json(value: list[str] | dict | None) -> str | None:
    """Serialize a list or dict column value, preserving None."""
    if value is None:
        return None
    return json.dumps(value)


def _to_date(value: str | None) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` string into a date.

    Sources emit an empty string when the source system provided no date, and
    a malformed value is possible from any external API. Both yield None
    rather than failing the item: a missing publication date is not a
    normalization error, and §4.2 declares the column nullable.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        logger.warning("published_date_unparseable", value=value)
        return None
