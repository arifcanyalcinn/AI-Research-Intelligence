"""
State machine for ARIP item lifecycle.

StateMachine.transition() is the ONLY place where item.status may be changed.
Direct assignment (item.status = X) anywhere else in the codebase is a bug.

The valid transition set is a frozenset of (from_status, to_status) tuples,
derived from the complete transition matrix in SDS Section 3.2 and the
detailed transition specifications in Section 3.3.

Note on the FAILED row: The transition matrix table in SDS 3.2 shows three
requeue targets for FAILED (COLLECTED, ENRICHED, APPROVED). Section 3.3
specifies two additional targets: RANKED (for EMBEDDING failures) and
GENERATED (for REVIEW_TIMEOUT). The more detailed Section 3.3 specification
takes precedence — all five are included in the frozenset.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from arip.enums import ItemStatus
from arip.exceptions import InvalidTransitionError

if TYPE_CHECKING:
    from arip.db.models import Item
    from arip.db.repositories.items import ItemRepository

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Valid transitions: the complete set from SDS Section 3
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: frozenset[tuple[ItemStatus, ItemStatus]] = frozenset(
    {
        # ── COLLECTED ────────────────────────────────────────────────────
        # norm_ok + score above threshold → RANKED
        (ItemStatus.COLLECTED, ItemStatus.RANKED),
        # norm_ok + score below threshold → FILTERED (terminal)
        (ItemStatus.COLLECTED, ItemStatus.FILTERED),
        # norm_error → FAILED
        (ItemStatus.COLLECTED, ItemStatus.FAILED),
        # ── RANKED ───────────────────────────────────────────────────────
        # embed_ok → EMBEDDED
        (ItemStatus.RANKED, ItemStatus.EMBEDDED),
        # embed_error → FAILED
        (ItemStatus.RANKED, ItemStatus.FAILED),
        # ── EMBEDDED ─────────────────────────────────────────────────────
        # dedup_pass → ENRICHED
        (ItemStatus.EMBEDDED, ItemStatus.ENRICHED),
        # dedup_hit → DUPLICATE (terminal)
        (ItemStatus.EMBEDDED, ItemStatus.DUPLICATE),
        # embed_error during ANN update → FAILED
        (ItemStatus.EMBEDDED, ItemStatus.FAILED),
        # ── ENRICHED ─────────────────────────────────────────────────────
        # gen_ok (generation + validation succeeded) → GENERATED
        (ItemStatus.ENRICHED, ItemStatus.GENERATED),
        # gen_max_retry (all generation retries exhausted) → FAILED
        (ItemStatus.ENRICHED, ItemStatus.FAILED),
        # ── GENERATED ────────────────────────────────────────────────────
        # review_enabled → PENDING_REVIEW
        (ItemStatus.GENERATED, ItemStatus.PENDING_REVIEW),
        # review_disabled (auto-approve) → APPROVED
        (ItemStatus.GENERATED, ItemStatus.APPROVED),
        # ── PENDING_REVIEW ────────────────────────────────────────────────
        # regen_request: reviewer asked for regeneration → back to ENRICHED
        (ItemStatus.PENDING_REVIEW, ItemStatus.ENRICHED),
        # reviewer_approve → APPROVED
        (ItemStatus.PENDING_REVIEW, ItemStatus.APPROVED),
        # reviewer_reject OR review_timeout → FAILED
        (ItemStatus.PENDING_REVIEW, ItemStatus.FAILED),
        # ── APPROVED ─────────────────────────────────────────────────────
        # publish_ok (at least one publisher succeeded) → PUBLISHED
        (ItemStatus.APPROVED, ItemStatus.PUBLISHED),
        # publish_max_retry (all publishers failed) → FAILED
        (ItemStatus.APPROVED, ItemStatus.FAILED),
        # ── PUBLISHED ────────────────────────────────────────────────────
        # archive_job → ARCHIVED (terminal)
        (ItemStatus.PUBLISHED, ItemStatus.ARCHIVED),
        # ── MANUAL RE-QUEUE (from terminal states) ────────────────────────
        # DUPLICATE → COLLECTED (restart from scratch)
        (ItemStatus.DUPLICATE, ItemStatus.COLLECTED),
        # FILTERED → COLLECTED (restart from scratch)
        (ItemStatus.FILTERED, ItemStatus.COLLECTED),
        # FAILED → COLLECTED  (failed_at_stage = NORMALIZATION or REVIEW/restart)
        (ItemStatus.FAILED, ItemStatus.COLLECTED),
        # FAILED → RANKED     (failed_at_stage = EMBEDDING)
        (ItemStatus.FAILED, ItemStatus.RANKED),
        # FAILED → ENRICHED   (failed_at_stage = GENERATION, VALIDATION, or REVIEW/regen)
        (ItemStatus.FAILED, ItemStatus.ENRICHED),
        # FAILED → GENERATED  (failed_at_stage = REVIEW_TIMEOUT → re-submit)
        (ItemStatus.FAILED, ItemStatus.GENERATED),
        # FAILED → APPROVED   (failed_at_stage = PUBLISHING → skip to publish)
        (ItemStatus.FAILED, ItemStatus.APPROVED),
    }
)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class StateMachine:
    """Enforces valid item lifecycle transitions.

    Construction requires an ItemRepository so that status updates are
    persisted atomically within the caller's session. The caller is responsible
    for committing the session after the transition.

    Usage:
        with session_scope(session_factory) as session:
            item_repo = ItemRepository(session)
            state_machine = StateMachine(item_repo)
            state_machine.transition(item, ItemStatus.RANKED)
            # session auto-commits on context manager exit
    """

    def __init__(self, item_repo: ItemRepository) -> None:
        self._item_repo = item_repo

    def transition(
        self,
        item: Item,
        target_status: ItemStatus,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Transition an item to a new status.

        Validates that the (from, to) pair is in VALID_TRANSITIONS, updates
        the item in the database, updates the in-memory ORM object, and logs
        the transition at INFO level.

        Args:
            item: The Item ORM object to transition.
            target_status: The desired new status.
            context: Optional dict of additional fields to update on the item
                     alongside the status change (e.g. failed_at_stage,
                     failure_reason, importance_score, normalized_at, etc.).

        Raises:
            InvalidTransitionError: If the (current_status → target_status)
                                    pair is not in VALID_TRANSITIONS.
        """
        from_status = ItemStatus(item.status)
        pair = (from_status, target_status)

        if pair not in VALID_TRANSITIONS:
            raise InvalidTransitionError(
                f"Forbidden state transition: {from_status.value} → {target_status.value} "
                f"(item_id={item.id}). "
                f"This is a bug in the calling stage — check the transition matrix."
            )

        # Apply the status update and any additional context fields.
        extra_fields = dict(context) if context else {}

        # Automatically set the appropriate timestamp if not provided.
        timestamp_field = _TIMESTAMP_FIELDS.get(target_status)
        if timestamp_field and timestamp_field not in extra_fields:
            extra_fields[timestamp_field] = datetime.utcnow()

        # Persist via repository (keeps in-memory object consistent with DB).
        self._item_repo.update_status(item.id, target_status, **extra_fields)

        # Update the in-memory ORM object to reflect the committed state.
        item.status = target_status.value
        for field_name, value in extra_fields.items():
            if hasattr(item, field_name):
                setattr(item, field_name, value)

        logger.info(
            "state_transition",
            item_id=item.id,
            from_status=from_status.value,
            to_status=target_status.value,
            source_id=item.source_id,
        )

    @staticmethod
    def is_valid_transition(from_status: ItemStatus, to_status: ItemStatus) -> bool:
        """Check whether a transition is valid without raising.

        Useful for pre-flight validation in tests and diagnostic tooling.
        """
        return (from_status, to_status) in VALID_TRANSITIONS


# ---------------------------------------------------------------------------
# Mapping from target status → the timestamp column to auto-populate.
# The stage code can override these by passing them in context.
# ---------------------------------------------------------------------------

_TIMESTAMP_FIELDS: dict[ItemStatus, str] = {
    ItemStatus.RANKED: "ranked_at",
    ItemStatus.ENRICHED: "enriched_at",
    ItemStatus.GENERATED: "generated_at",
    ItemStatus.PENDING_REVIEW: "review_submitted_at",
    ItemStatus.APPROVED: "approved_at",
    ItemStatus.PUBLISHED: "published_at",
    ItemStatus.ARCHIVED: "archived_at",
}
