"""
Unit tests for ItemRepository.

Tests all 8 methods against an in-memory SQLite database.
No mocking of the database layer — test against real (transient) SQLite.

SDS Phase 0 quality gate: ItemRepository.create() and get_by_id() tested
against :memory: SQLite. This file covers all 8 methods.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from arip.db.database import init_db
from arip.db.repositories.items import ItemRepository
from arip.enums import ItemStatus

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
def repo(db_session: Session) -> ItemRepository:
    return ItemRepository(db_session)


def make_item_data(
    source_id: str = "arxiv",
    external_id: str | None = None,
    status: ItemStatus = ItemStatus.COLLECTED,
    language: str = "EN",
) -> dict:
    """Build a minimal valid dict for ItemRepository.create()."""
    if external_id is None:
        external_id = str(uuid.uuid4())
    return {
        "source_id": source_id,
        "source_type": "PAPER",
        "external_id": external_id,
        "content_hash": f"hash-{uuid.uuid4()}",
        "status": status.value,
        "language": language,
        "title": "Test Paper Title",
        "primary_url": "https://arxiv.org/abs/test",
        "collected_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


def test_create_returns_item_with_id(repo: ItemRepository, db_session: Session) -> None:
    """create() inserts a row and returns an Item with a populated id."""
    item = repo.create(make_item_data())
    assert item.id is not None
    assert isinstance(item.id, int)
    assert item.id > 0


def test_create_auto_generates_uuid(repo: ItemRepository) -> None:
    """create() auto-generates a UUID if not provided."""
    data = make_item_data()
    data.pop("uuid", None)
    item = repo.create(data)
    assert item.uuid is not None
    # Verify it's a valid UUID string
    uuid.UUID(item.uuid)


def test_create_uses_provided_uuid(repo: ItemRepository) -> None:
    """create() respects a UUID if explicitly provided."""
    my_uuid = str(uuid.uuid4())
    data = make_item_data()
    data["uuid"] = my_uuid
    item = repo.create(data)
    assert item.uuid == my_uuid


def test_create_sets_default_status(repo: ItemRepository) -> None:
    """create() defaults status to COLLECTED if not provided."""
    data = make_item_data()
    data.pop("status", None)
    item = repo.create(data)
    assert item.status == ItemStatus.COLLECTED.value


def test_create_raises_on_duplicate_content_hash(
    repo: ItemRepository, db_session: Session
) -> None:
    """Duplicate content_hash violates the unique constraint."""
    from sqlalchemy.exc import IntegrityError

    data = make_item_data()
    repo.create(data)
    db_session.flush()

    # Same content_hash — different external_id doesn't matter
    duplicate = make_item_data()
    duplicate["content_hash"] = data["content_hash"]
    duplicate["external_id"] = str(uuid.uuid4())

    with pytest.raises(IntegrityError):
        repo.create(duplicate)
        db_session.flush()


# ---------------------------------------------------------------------------
# get_by_id()
# ---------------------------------------------------------------------------


def test_get_by_id_returns_item(repo: ItemRepository, db_session: Session) -> None:
    """get_by_id() returns the correct item."""
    item = repo.create(make_item_data())
    db_session.flush()

    found = repo.get_by_id(item.id)
    assert found is not None
    assert found.id == item.id
    assert found.source_id == "arxiv"


def test_get_by_id_returns_none_for_missing(repo: ItemRepository) -> None:
    """get_by_id() returns None when the item doesn't exist."""
    assert repo.get_by_id(99999) is None


# ---------------------------------------------------------------------------
# get_by_content_hash()
# ---------------------------------------------------------------------------


def test_get_by_content_hash_finds_item(repo: ItemRepository, db_session: Session) -> None:
    data = make_item_data()
    item = repo.create(data)
    db_session.flush()

    found = repo.get_by_content_hash(item.content_hash)
    assert found is not None
    assert found.id == item.id


def test_get_by_content_hash_returns_none_for_missing(repo: ItemRepository) -> None:
    assert repo.get_by_content_hash("nonexistent-hash") is None


# ---------------------------------------------------------------------------
# get_by_status()
# ---------------------------------------------------------------------------


def test_get_by_status_returns_correct_items(
    repo: ItemRepository, db_session: Session
) -> None:
    """get_by_status() returns only items with that status."""
    repo.create(make_item_data(status=ItemStatus.COLLECTED))
    repo.create(make_item_data(status=ItemStatus.COLLECTED))
    repo.create(make_item_data(status=ItemStatus.RANKED))
    db_session.flush()

    collected = repo.get_by_status(ItemStatus.COLLECTED)
    assert len(collected) == 2
    assert all(i.status == ItemStatus.COLLECTED.value for i in collected)

    ranked = repo.get_by_status(ItemStatus.RANKED)
    assert len(ranked) == 1


def test_get_by_status_returns_empty_list(repo: ItemRepository) -> None:
    """get_by_status() returns [] when no items match."""
    result = repo.get_by_status(ItemStatus.PUBLISHED)
    assert result == []


# ---------------------------------------------------------------------------
# get_by_status_and_language()
# ---------------------------------------------------------------------------


def test_get_by_status_and_language_filters_correctly(
    repo: ItemRepository, db_session: Session
) -> None:
    """Returns only items matching both status and language."""
    repo.create(make_item_data(status=ItemStatus.RANKED, language="EN"))
    repo.create(make_item_data(status=ItemStatus.RANKED, language="TR"))
    repo.create(make_item_data(status=ItemStatus.COLLECTED, language="EN"))
    db_session.flush()

    en_ranked = repo.get_by_status_and_language(ItemStatus.RANKED, "EN")
    assert len(en_ranked) == 1
    assert en_ranked[0].language == "EN"

    tr_ranked = repo.get_by_status_and_language(ItemStatus.RANKED, "TR")
    assert len(tr_ranked) == 1
    assert tr_ranked[0].language == "TR"


# ---------------------------------------------------------------------------
# get_by_source_and_external_id()
# ---------------------------------------------------------------------------


def test_get_by_source_and_external_id_finds_item(
    repo: ItemRepository, db_session: Session
) -> None:
    data = make_item_data(source_id="arxiv", external_id="2401.12345")
    item = repo.create(data)
    db_session.flush()

    found = repo.get_by_source_and_external_id("arxiv", "2401.12345")
    assert found is not None
    assert found.id == item.id


def test_get_by_source_and_external_id_returns_none(repo: ItemRepository) -> None:
    result = repo.get_by_source_and_external_id("arxiv", "nonexistent")
    assert result is None


# ---------------------------------------------------------------------------
# update_status()
# ---------------------------------------------------------------------------


def test_update_status_changes_status(repo: ItemRepository, db_session: Session) -> None:
    item = repo.create(make_item_data())
    db_session.flush()

    repo.update_status(item.id, ItemStatus.RANKED)
    db_session.flush()

    refreshed = repo.get_by_id(item.id)
    assert refreshed.status == ItemStatus.RANKED.value


def test_update_status_with_extra_fields(repo: ItemRepository, db_session: Session) -> None:
    """update_status() can update additional columns alongside status."""
    item = repo.create(make_item_data())
    db_session.flush()

    repo.update_status(
        item.id,
        ItemStatus.FAILED,
        failed_at_stage="NORMALIZATION",
        failure_reason="Missing title",
        retry_count=1,
    )
    db_session.flush()

    refreshed = repo.get_by_id(item.id)
    assert refreshed.status == ItemStatus.FAILED.value
    assert refreshed.failed_at_stage == "NORMALIZATION"
    assert refreshed.failure_reason == "Missing title"
    assert refreshed.retry_count == 1


def test_update_status_sets_updated_at(repo: ItemRepository, db_session: Session) -> None:
    """update_status() always refreshes updated_at."""
    item = repo.create(make_item_data())
    original_updated_at = item.updated_at
    db_session.flush()

    repo.update_status(item.id, ItemStatus.RANKED)
    db_session.flush()

    refreshed = repo.get_by_id(item.id)
    assert refreshed.updated_at >= original_updated_at


def test_update_status_nonexistent_item_does_not_raise(repo: ItemRepository) -> None:
    """update_status() on a missing item logs and returns gracefully."""
    # Should not raise
    repo.update_status(99999, ItemStatus.RANKED)


# ---------------------------------------------------------------------------
# get_pending_review()
# ---------------------------------------------------------------------------


def test_get_pending_review_returns_timed_out_items(
    repo: ItemRepository, db_session: Session
) -> None:
    """Returns items in PENDING_REVIEW older than timeout_hours."""
    # Item submitted 25 hours ago (beyond 24h timeout)
    old_item_data = make_item_data(status=ItemStatus.PENDING_REVIEW)
    old_item_data["review_submitted_at"] = datetime.utcnow() - timedelta(hours=25)
    old_item = repo.create(old_item_data)
    db_session.flush()

    # Item submitted 1 hour ago (within timeout)
    new_item_data = make_item_data(status=ItemStatus.PENDING_REVIEW)
    new_item_data["review_submitted_at"] = datetime.utcnow() - timedelta(hours=1)
    repo.create(new_item_data)
    db_session.flush()

    timed_out = repo.get_pending_review(timeout_hours=24)
    assert len(timed_out) == 1
    assert timed_out[0].id == old_item.id


def test_get_pending_review_returns_empty_when_none_timed_out(
    repo: ItemRepository, db_session: Session
) -> None:
    item_data = make_item_data(status=ItemStatus.PENDING_REVIEW)
    item_data["review_submitted_at"] = datetime.utcnow() - timedelta(hours=1)
    repo.create(item_data)
    db_session.flush()

    result = repo.get_pending_review(timeout_hours=24)
    assert result == []


# ---------------------------------------------------------------------------
# get_all_with_embeddings()
# ---------------------------------------------------------------------------


def test_get_all_with_embeddings_excludes_failed_duplicate_filtered(
    repo: ItemRepository, db_session: Session
) -> None:
    """Items with status FAILED/DUPLICATE/FILTERED are excluded even if embedded."""
    now = datetime.utcnow()

    # Embedded RANKED item — should be included
    ranked_data = make_item_data(status=ItemStatus.RANKED)
    ranked_data["embedding_computed_at"] = now
    ranked_data["embedding_model_name"] = "all-MiniLM-L6-v2"
    ranked_item = repo.create(ranked_data)

    # Embedded FAILED item — should be excluded
    failed_data = make_item_data(status=ItemStatus.FAILED)
    failed_data["embedding_computed_at"] = now
    repo.create(failed_data)

    # Embedded DUPLICATE item — should be excluded
    dup_data = make_item_data(status=ItemStatus.DUPLICATE)
    dup_data["embedding_computed_at"] = now
    repo.create(dup_data)

    # Not embedded at all — should be excluded
    repo.create(make_item_data(status=ItemStatus.RANKED))

    db_session.flush()

    result = repo.get_all_with_embeddings()
    assert len(result) == 1
    assert result[0].id == ranked_item.id
