"""
Unit tests for StateMachine.

SDS Phase 0 requirement:
  "All tests for the state machine are written in Phase 0. Tests:
   - Every valid transition (every row in the transition matrix): passes
   - Representative invalid transitions: raises InvalidTransitionError
   - manual_requeue transitions: correct re-queue targets per failed_at_stage"

Test strategy: use an in-memory SQLite DB with tables created from ORM
metadata (init_db). No mocking — the state machine talks to a real
(but transient) ItemRepository.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from arip.db.database import init_db
from arip.db.models import Item
from arip.db.repositories.items import ItemRepository
from arip.enums import ItemStatus
from arip.exceptions import InvalidTransitionError
from arip.state_machine import VALID_TRANSITIONS, StateMachine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def db_session() -> Session:
    """In-memory SQLite session with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def item_repo(db_session: Session) -> ItemRepository:
    return ItemRepository(db_session)


@pytest.fixture
def state_machine(item_repo: ItemRepository) -> StateMachine:
    return StateMachine(item_repo)


def make_item(session: Session, status: ItemStatus = ItemStatus.COLLECTED) -> Item:
    """Insert a minimal Item into the in-memory DB and return it."""
    import uuid

    item = Item(
        uuid=str(uuid.uuid4()),
        source_id="test_source",
        source_type="PAPER",
        external_id=f"test-{uuid.uuid4()}",
        content_hash=f"hash-{uuid.uuid4()}",
        status=status.value,
        language="EN",
        collected_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(item)
    session.flush()
    return item


# ---------------------------------------------------------------------------
# Test: VALID_TRANSITIONS frozenset is non-empty and well-formed
# ---------------------------------------------------------------------------


def test_valid_transitions_set_is_not_empty() -> None:
    assert len(VALID_TRANSITIONS) > 0


def test_valid_transitions_no_self_loops() -> None:
    """No state should transition to itself — that would be a no-op."""
    for from_s, to_s in VALID_TRANSITIONS:
        assert from_s != to_s, f"Self-loop detected: {from_s} → {to_s}"


def test_archived_has_no_outgoing_transitions() -> None:
    """ARCHIVED is a strictly terminal state."""
    outgoing = [pair for pair in VALID_TRANSITIONS if pair[0] == ItemStatus.ARCHIVED]
    assert outgoing == [], f"ARCHIVED should have no outgoing transitions, got: {outgoing}"


# ---------------------------------------------------------------------------
# Test: every valid transition in the matrix passes
# ---------------------------------------------------------------------------


ALL_VALID = list(VALID_TRANSITIONS)


@pytest.mark.parametrize("from_status,to_status", ALL_VALID, ids=lambda s: s.value)
def test_valid_transition_succeeds(
    from_status: ItemStatus,
    to_status: ItemStatus,
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """Every (from, to) pair in VALID_TRANSITIONS should complete without error."""
    item = make_item(db_session, from_status)

    # Should not raise.
    state_machine.transition(item, to_status)

    assert item.status == to_status.value


# ---------------------------------------------------------------------------
# Test: representative INVALID transitions raise InvalidTransitionError
# ---------------------------------------------------------------------------


INVALID_TRANSITIONS = [
    # Forward jumps that skip stages
    (ItemStatus.COLLECTED, ItemStatus.PUBLISHED),
    (ItemStatus.COLLECTED, ItemStatus.ARCHIVED),
    (ItemStatus.RANKED, ItemStatus.GENERATED),
    (ItemStatus.EMBEDDED, ItemStatus.APPROVED),
    (ItemStatus.ENRICHED, ItemStatus.PUBLISHED),
    # Backward transitions (except the allowed PENDING_REVIEW → ENRICHED)
    (ItemStatus.GENERATED, ItemStatus.COLLECTED),
    (ItemStatus.APPROVED, ItemStatus.COLLECTED),
    (ItemStatus.PUBLISHED, ItemStatus.COLLECTED),
    # Terminal states that should have no outgoing normal transitions
    (ItemStatus.ARCHIVED, ItemStatus.COLLECTED),
    (ItemStatus.ARCHIVED, ItemStatus.FAILED),
    # DUPLICATE can only go to COLLECTED (manual requeue), not anywhere else
    (ItemStatus.DUPLICATE, ItemStatus.RANKED),
    (ItemStatus.DUPLICATE, ItemStatus.PUBLISHED),
    # FILTERED can only go to COLLECTED, not anywhere else
    (ItemStatus.FILTERED, ItemStatus.GENERATED),
]


@pytest.mark.parametrize("from_status,to_status", INVALID_TRANSITIONS)
def test_invalid_transition_raises(
    from_status: ItemStatus,
    to_status: ItemStatus,
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """Transitions not in VALID_TRANSITIONS must raise InvalidTransitionError."""
    item = make_item(db_session, from_status)

    with pytest.raises(InvalidTransitionError) as exc_info:
        state_machine.transition(item, to_status)

    # Error message must identify the from and to states.
    error_message = str(exc_info.value)
    assert from_status.value in error_message
    assert to_status.value in error_message


# ---------------------------------------------------------------------------
# Test: InvalidTransitionError does NOT change item status
# ---------------------------------------------------------------------------


def test_failed_transition_does_not_change_status(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """When a transition raises, item.status must remain unchanged."""
    item = make_item(db_session, ItemStatus.COLLECTED)
    original_status = item.status

    with pytest.raises(InvalidTransitionError):
        state_machine.transition(item, ItemStatus.PUBLISHED)

    assert item.status == original_status


# ---------------------------------------------------------------------------
# Test: is_valid_transition static helper
# ---------------------------------------------------------------------------


def test_is_valid_transition_known_valid() -> None:
    assert StateMachine.is_valid_transition(ItemStatus.COLLECTED, ItemStatus.RANKED) is True


def test_is_valid_transition_known_invalid() -> None:
    assert StateMachine.is_valid_transition(ItemStatus.COLLECTED, ItemStatus.PUBLISHED) is False


# ---------------------------------------------------------------------------
# Test: manual requeue transitions (FAILED → correct re-queue target)
# ---------------------------------------------------------------------------


def test_failed_normalization_requeues_to_collected(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """FAILED(NORMALIZATION) → COLLECTED (full restart)."""
    item = make_item(db_session, ItemStatus.FAILED)
    item.failed_at_stage = "NORMALIZATION"

    state_machine.transition(item, ItemStatus.COLLECTED)
    assert item.status == ItemStatus.COLLECTED.value


def test_failed_embedding_requeues_to_ranked(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """FAILED(EMBEDDING) → RANKED (skip collection)."""
    item = make_item(db_session, ItemStatus.FAILED)
    item.failed_at_stage = "EMBEDDING"

    state_machine.transition(item, ItemStatus.RANKED)
    assert item.status == ItemStatus.RANKED.value


def test_failed_generation_requeues_to_enriched(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """FAILED(GENERATION) → ENRICHED (skip collection/ranking/dedup)."""
    item = make_item(db_session, ItemStatus.FAILED)
    item.failed_at_stage = "GENERATION"

    state_machine.transition(item, ItemStatus.ENRICHED)
    assert item.status == ItemStatus.ENRICHED.value


def test_failed_review_timeout_requeues_to_generated(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """FAILED(REVIEW_TIMEOUT) → GENERATED (re-submit to reviewer)."""
    item = make_item(db_session, ItemStatus.FAILED)
    item.failed_at_stage = "REVIEW_TIMEOUT"

    state_machine.transition(item, ItemStatus.GENERATED)
    assert item.status == ItemStatus.GENERATED.value


def test_failed_publishing_requeues_to_approved(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """FAILED(PUBLISHING) → APPROVED (skip straight to publishing)."""
    item = make_item(db_session, ItemStatus.FAILED)
    item.failed_at_stage = "PUBLISHING"

    state_machine.transition(item, ItemStatus.APPROVED)
    assert item.status == ItemStatus.APPROVED.value


def test_duplicate_requeues_to_collected(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """DUPLICATE → COLLECTED (manual re-queue)."""
    item = make_item(db_session, ItemStatus.DUPLICATE)

    state_machine.transition(item, ItemStatus.COLLECTED)
    assert item.status == ItemStatus.COLLECTED.value


def test_filtered_requeues_to_collected(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """FILTERED → COLLECTED (manual re-queue)."""
    item = make_item(db_session, ItemStatus.FILTERED)

    state_machine.transition(item, ItemStatus.COLLECTED)
    assert item.status == ItemStatus.COLLECTED.value


# ---------------------------------------------------------------------------
# Test: backward transition PENDING_REVIEW → ENRICHED (regen_request)
# ---------------------------------------------------------------------------


def test_regen_request_goes_back_to_enriched(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """PENDING_REVIEW → ENRICHED is the only allowed backward transition."""
    item = make_item(db_session, ItemStatus.PENDING_REVIEW)

    state_machine.transition(item, ItemStatus.ENRICHED)
    assert item.status == ItemStatus.ENRICHED.value


# ---------------------------------------------------------------------------
# Test: context fields are persisted alongside status
# ---------------------------------------------------------------------------


def test_transition_with_context_persists_fields(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """Context dict fields (e.g. failure_reason) are saved to the DB."""
    item = make_item(db_session, ItemStatus.COLLECTED)

    state_machine.transition(
        item,
        ItemStatus.FAILED,
        context={
            "failed_at_stage": "NORMALIZATION",
            "failure_reason": "Missing title field",
        },
    )

    db_session.flush()
    refreshed = item_repo.get_by_id(item.id)
    assert refreshed is not None
    assert refreshed.status == ItemStatus.FAILED.value
    assert refreshed.failed_at_stage == "NORMALIZATION"
    assert refreshed.failure_reason == "Missing title field"


# ---------------------------------------------------------------------------
# Test: auto-timestamp population
# ---------------------------------------------------------------------------


def test_transition_auto_sets_timestamp(
    db_session: Session,
    item_repo: ItemRepository,
    state_machine: StateMachine,
) -> None:
    """Transitioning to RANKED auto-populates ranked_at if not in context."""
    item = make_item(db_session, ItemStatus.COLLECTED)
    assert item.ranked_at is None

    state_machine.transition(item, ItemStatus.RANKED)

    assert item.ranked_at is not None
    assert isinstance(item.ranked_at, datetime)
