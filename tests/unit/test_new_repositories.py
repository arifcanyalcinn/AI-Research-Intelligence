"""
Unit tests for RawPayloadRepository and PipelineRunRepository.

Both repositories are introduced by SDS §5.14.1 to own the two tables that
§5.14's original four-repository list left without an owner:
``raw_source_payloads`` (§4.7) and ``pipeline_runs`` (§4.6).

Tested against an in-memory SQLite database, following the convention already
established by tests/unit/test_repositories.py — no mocking of the database
layer.

Tests assert behaviour required by the SDS, not private implementation
details: rows are persisted with the right column values, the §4.6 startup
concurrency guard reconciles crashed runs, and neither repository commits the
caller's session.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from arip.db.database import init_db
from arip.db.models import PipelineRun, RawSourcePayload
from arip.db.repositories.items import ItemRepository
from arip.db.repositories.pipeline_runs import PipelineRunRepository
from arip.db.repositories.raw_payloads import RawPayloadRepository
from arip.enums import ItemStatus, PipelineRunStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def db_session() -> Session:
    """Fresh in-memory SQLite session for each test."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def payload_repo(db_session: Session) -> RawPayloadRepository:
    return RawPayloadRepository(db_session)


@pytest.fixture
def run_repo(db_session: Session) -> PipelineRunRepository:
    return PipelineRunRepository(db_session)


@pytest.fixture
def item_id(db_session: Session) -> int:
    """Create a parent item and return its id.

    raw_source_payloads.item_id is a foreign key to items.id, so a real parent
    row is required.
    """
    repo = ItemRepository(db_session)
    item = repo.create(
        {
            "source_id": "arxiv",
            "source_type": "PAPER",
            "external_id": str(uuid.uuid4()),
            "content_hash": f"hash-{uuid.uuid4()}",
            "status": ItemStatus.COLLECTED.value,
            "language": "EN",
            "title": "Test Paper Title",
            "primary_url": "https://arxiv.org/abs/test",
        }
    )
    return item.id


# ---------------------------------------------------------------------------
# RawPayloadRepository.create()
# ---------------------------------------------------------------------------


def test_create_payload_returns_row_with_id(
    payload_repo: RawPayloadRepository, item_id: int
) -> None:
    """create() inserts a row and returns it with a populated id."""
    row = payload_repo.create(
        {"item_id": item_id, "source_id": "arxiv", "payload": "{}"}
    )
    assert row.id is not None
    assert row.id > 0


def test_create_payload_persists_all_given_fields(
    payload_repo: RawPayloadRepository, db_session: Session, item_id: int
) -> None:
    """The stored row carries the item_id, source_id and payload supplied."""
    raw = json.dumps({"title": "A paper", "id": "2401.12345"})
    row = payload_repo.create(
        {"item_id": item_id, "source_id": "arxiv", "payload": raw}
    )

    stored = db_session.get(RawSourcePayload, row.id)
    assert stored.item_id == item_id
    assert stored.source_id == "arxiv"
    assert stored.payload == raw


def test_payload_round_trips_as_json(
    payload_repo: RawPayloadRepository, db_session: Session, item_id: int
) -> None:
    """A JSON payload survives storage and can be parsed back (SDS §4.7 replay)."""
    original = {"title": "Curly {braces} and \"quotes\"", "authors": ["A", "B"]}
    row = payload_repo.create(
        {
            "item_id": item_id,
            "source_id": "arxiv",
            "payload": json.dumps(original),
        }
    )

    stored = db_session.get(RawSourcePayload, row.id)
    assert json.loads(stored.payload) == original


def test_create_payload_defaults_fetched_at(
    payload_repo: RawPayloadRepository, item_id: int
) -> None:
    """fetched_at is populated automatically when the caller omits it."""
    row = payload_repo.create(
        {"item_id": item_id, "source_id": "arxiv", "payload": "{}"}
    )
    assert isinstance(row.fetched_at, datetime)


def test_create_payload_honours_explicit_fetched_at(
    payload_repo: RawPayloadRepository, item_id: int
) -> None:
    """An explicitly supplied fetched_at is not overwritten."""
    when = datetime(2026, 1, 2, 3, 4, 5)
    row = payload_repo.create(
        {
            "item_id": item_id,
            "source_id": "arxiv",
            "payload": "{}",
            "fetched_at": when,
        }
    )
    assert row.fetched_at == when


def test_fetch_url_is_null_when_absent(
    payload_repo: RawPayloadRepository, item_id: int
) -> None:
    """fetch_url stays NULL when not supplied — the documented TD-012 state."""
    row = payload_repo.create(
        {"item_id": item_id, "source_id": "arxiv", "payload": "{}"}
    )
    assert row.fetch_url is None


def test_create_payload_does_not_commit(
    payload_repo: RawPayloadRepository, db_session: Session, item_id: int
) -> None:
    """The repository flushes but never commits — the caller owns the session."""
    payload_repo.create({"item_id": item_id, "source_id": "arxiv", "payload": "{}"})
    assert db_session.in_transaction()


def test_multiple_payloads_for_one_item(
    payload_repo: RawPayloadRepository, item_id: int
) -> None:
    """The table accepts more than one payload row per item."""
    first = payload_repo.create(
        {"item_id": item_id, "source_id": "arxiv", "payload": '{"n": 1}'}
    )
    second = payload_repo.create(
        {"item_id": item_id, "source_id": "arxiv", "payload": '{"n": 2}'}
    )
    assert first.id != second.id


# ---------------------------------------------------------------------------
# PipelineRunRepository.create()
# ---------------------------------------------------------------------------


def test_create_run_returns_row_with_id(run_repo: PipelineRunRepository) -> None:
    """create() inserts a run and returns it with a populated id."""
    run = run_repo.create()
    assert run.id is not None
    assert run.id > 0


def test_create_run_status_is_running(run_repo: PipelineRunRepository) -> None:
    """A newly opened run is RUNNING (SDS §4.6)."""
    run = run_repo.create()
    assert run.status == PipelineRunStatus.RUNNING.value


def test_create_run_sets_started_at(run_repo: PipelineRunRepository) -> None:
    """started_at is stamped on creation."""
    run = run_repo.create()
    assert isinstance(run.started_at, datetime)


def test_create_run_leaves_completed_at_null(run_repo: PipelineRunRepository) -> None:
    """An open run has no completion time yet."""
    run = run_repo.create()
    assert run.completed_at is None


def test_stage_metrics_is_null_on_creation(run_repo: PipelineRunRepository) -> None:
    """stage_metrics stays NULL — it is a Phase 7 deliverable (SDS §8)."""
    run = run_repo.create()
    assert run.stage_metrics is None


# ---------------------------------------------------------------------------
# PipelineRunRepository.complete()
# ---------------------------------------------------------------------------


def test_complete_sets_status_completed(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """complete() moves the run to COMPLETED."""
    run = run_repo.create()
    run_repo.complete(run.id)

    stored = db_session.get(PipelineRun, run.id)
    assert stored.status == PipelineRunStatus.COMPLETED.value


def test_complete_stamps_completed_at(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """complete() records when the run finished."""
    run = run_repo.create()
    run_repo.complete(run.id)

    stored = db_session.get(PipelineRun, run.id)
    assert isinstance(stored.completed_at, datetime)


def test_complete_unknown_run_id_does_not_raise(
    run_repo: PipelineRunRepository,
) -> None:
    """A missing run is logged, not raised — matches ItemRepository.update_status()."""
    run_repo.complete(999_999)


# ---------------------------------------------------------------------------
# PipelineRunRepository.fail()
# ---------------------------------------------------------------------------


def test_fail_sets_status_failed(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """fail() moves the run to FAILED."""
    run = run_repo.create()
    run_repo.fail(run.id, "collection raised")

    stored = db_session.get(PipelineRun, run.id)
    assert stored.status == PipelineRunStatus.FAILED.value


def test_fail_records_error_summary(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """The reason is stored in pipeline_runs.error_summary (SDS §4.6)."""
    run = run_repo.create()
    run_repo.fail(run.id, "collection raised")

    stored = db_session.get(PipelineRun, run.id)
    assert stored.error_summary == "collection raised"


def test_fail_stamps_completed_at(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """A run that failed during execution did finish, at a known time."""
    run = run_repo.create()
    run_repo.fail(run.id, "boom")

    stored = db_session.get(PipelineRun, run.id)
    assert isinstance(stored.completed_at, datetime)


def test_fail_unknown_run_id_does_not_raise(run_repo: PipelineRunRepository) -> None:
    """A missing run is logged, not raised."""
    run_repo.fail(999_999, "boom")


# ---------------------------------------------------------------------------
# PipelineRunRepository.reconcile_crashed() — SDS §4.6 startup guard
# ---------------------------------------------------------------------------


def test_reconcile_returns_zero_when_no_stale_runs(
    run_repo: PipelineRunRepository,
) -> None:
    """A clean previous shutdown leaves nothing to reconcile."""
    assert run_repo.reconcile_crashed() == 0


def test_reconcile_marks_running_run_failed(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """A run left RUNNING is the residue of a crash and becomes FAILED."""
    run = run_repo.create()

    count = run_repo.reconcile_crashed()

    stored = db_session.get(PipelineRun, run.id)
    assert count == 1
    assert stored.status == PipelineRunStatus.FAILED.value


def test_reconcile_records_a_reason(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """The reconciled run explains why it is FAILED rather than leaving it blank."""
    run = run_repo.create()
    run_repo.reconcile_crashed()

    stored = db_session.get(PipelineRun, run.id)
    assert stored.error_summary is not None
    assert "RUNNING" in stored.error_summary


def test_reconcile_leaves_completed_at_null(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """The time of the crash is unknown, so no completion time is invented."""
    run = run_repo.create()
    run_repo.reconcile_crashed()

    stored = db_session.get(PipelineRun, run.id)
    assert stored.completed_at is None


def test_reconcile_ignores_completed_runs(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """A COMPLETED run is untouched by the startup guard."""
    run = run_repo.create()
    run_repo.complete(run.id)

    count = run_repo.reconcile_crashed()

    stored = db_session.get(PipelineRun, run.id)
    assert count == 0
    assert stored.status == PipelineRunStatus.COMPLETED.value


def test_reconcile_ignores_already_failed_runs(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """An already-FAILED run keeps its original error_summary."""
    run = run_repo.create()
    run_repo.fail(run.id, "original reason")

    count = run_repo.reconcile_crashed()

    stored = db_session.get(PipelineRun, run.id)
    assert count == 0
    assert stored.error_summary == "original reason"


def test_reconcile_handles_multiple_stale_runs(
    run_repo: PipelineRunRepository,
) -> None:
    """Every stale run is reconciled, and the count reflects that."""
    run_repo.create()
    run_repo.create()
    run_repo.create()

    assert run_repo.reconcile_crashed() == 3


def test_reconcile_is_idempotent(run_repo: PipelineRunRepository) -> None:
    """A second reconciliation finds nothing left to do."""
    run_repo.create()

    assert run_repo.reconcile_crashed() == 1
    assert run_repo.reconcile_crashed() == 0


def test_new_run_after_reconcile_is_running(
    run_repo: PipelineRunRepository, db_session: Session
) -> None:
    """The startup sequence — reconcile, then open a fresh run — works end to end."""
    crashed = run_repo.create()
    run_repo.reconcile_crashed()

    fresh = run_repo.create()

    assert db_session.get(PipelineRun, crashed.id).status == (
        PipelineRunStatus.FAILED.value
    )
    assert db_session.get(PipelineRun, fresh.id).status == (
        PipelineRunStatus.RUNNING.value
    )


# ---------------------------------------------------------------------------
# Defect 1 regression — items has no raw_payload column (SDS §4.7, TD-011)
# ---------------------------------------------------------------------------


def test_item_create_rejects_raw_payload(db_session: Session) -> None:
    """raw_payload is not a column on items; it belongs in raw_source_payloads.

    Guards the mismatch recorded as TD-011: NormalizedItem carries a
    raw_payload field, but SDS §4.7 removed the column from items. A collect
    stage that splats a NormalizedItem into ItemRepository.create() must fail
    loudly rather than silently dropping the payload.
    """
    repo = ItemRepository(db_session)
    with pytest.raises(TypeError):
        repo.create(
            {
                "source_id": "arxiv",
                "source_type": "PAPER",
                "external_id": str(uuid.uuid4()),
                "content_hash": f"hash-{uuid.uuid4()}",
                "title": "Test",
                "primary_url": "https://example.org/x",
                "raw_payload": "{}",
            }
        )
