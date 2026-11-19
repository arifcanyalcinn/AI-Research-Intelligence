"""
Unit tests for ItemRepository.

Tests all 8 methods against an in-memory SQLite database.
No mocking of the database layer — test against real (transient) SQLite.

SDS Phase 0 quality gate: ItemRepository.create() and get_by_id() tested
against :memory: SQLite. This file covers all 8 methods.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from arip.db.database import init_db
from arip.db.models import Item
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


def test_create_raises_on_duplicate_content_hash(repo: ItemRepository, db_session: Session) -> None:
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


def test_get_by_status_returns_correct_items(repo: ItemRepository, db_session: Session) -> None:
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


def test_get_by_status_orders_equal_collection_times_by_id(
    repo: ItemRepository, db_session: Session
) -> None:
    """T14 / D-009: collected_at, then id. SQL leaves ties undefined otherwise.

    The SQL is checked as well as the rows, because SQLite happens to return
    such ties in id order even without the key: the row order alone could not
    tell the two queries apart.
    """
    from sqlalchemy import event

    same = datetime(2026, 11, 1, 9, 0, 0)
    later = repo.create(
        {**make_item_data(status=ItemStatus.RANKED), "collected_at": same + timedelta(minutes=1)}
    )
    first = repo.create({**make_item_data(status=ItemStatus.RANKED), "collected_at": same})
    second = repo.create({**make_item_data(status=ItemStatus.RANKED), "collected_at": same})
    db_session.flush()

    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        ranked = repo.get_by_status(ItemStatus.RANKED)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert [i.id for i in ranked] == [first.id, second.id, later.id]
    order_by = statements[-1].split("ORDER BY", 1)[1].split()
    assert order_by == ["items.collected_at", "ASC,", "items.id", "ASC"]


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
# get_by_status_and_language() — selection order (D-010, TD-023 a)
#
# Every test inserts its items so that id order is the WRONG answer, so the
# key under test is what puts them right. The id test itself reverses the
# order SQLite returns rows in, because the query has no ORDER BY.
# ---------------------------------------------------------------------------


def add_selectable(
    repo: ItemRepository,
    *,
    score: float | None = 0.6,
    engagement: float | None = 0.5,
    topic: float | None = 0.5,
    published: date | None = date(2026, 10, 1),
    breakdown: str | None = None,
) -> int:
    """One ENRICHED EN item; ``breakdown`` overrides the JSON built from the signals."""
    if breakdown is None:
        signals = {"recency": 0.5, "authority": 0.5}
        if engagement is not None:
            signals["engagement"] = engagement
        if topic is not None:
            signals["topic"] = topic
        breakdown = json.dumps(signals)
    data = make_item_data(status=ItemStatus.ENRICHED)
    data.update(importance_score=score, signal_breakdown=breakdown, published_date=published)
    return repo.create(data).id


def selection_ids(repo: ItemRepository) -> list[int]:
    return [i.id for i in repo.get_by_status_and_language(ItemStatus.ENRICHED, "EN")]


def test_selection_order_puts_the_higher_score_first(repo: ItemRepository) -> None:
    """Catches: score ascending; score key dropped."""
    low = add_selectable(repo, score=0.5)
    high = add_selectable(repo, score=0.9)
    mid = add_selectable(repo, score=0.7)
    assert selection_ids(repo) == [high, mid, low]


def test_selection_order_breaks_score_ties_by_higher_engagement(repo: ItemRepository) -> None:
    """Catches: engagement key dropped; engagement ascending."""
    low = add_selectable(repo, engagement=0.2)
    high = add_selectable(repo, engagement=0.8)
    assert selection_ids(repo) == [high, low]


def test_selection_order_ranks_score_before_engagement(repo: ItemRepository) -> None:
    """Catches: engagement compared before score."""
    second = add_selectable(repo, score=0.8, engagement=0.9)
    first = add_selectable(repo, score=0.9, engagement=0.1)
    assert selection_ids(repo) == [first, second]


def test_selection_order_breaks_engagement_ties_by_higher_topic(repo: ItemRepository) -> None:
    """Catches: topic key dropped; topic ascending."""
    low = add_selectable(repo, topic=0.1)
    high = add_selectable(repo, topic=0.9)
    assert selection_ids(repo) == [high, low]


def test_selection_order_ranks_engagement_before_topic(repo: ItemRepository) -> None:
    """Catches: topic compared before engagement."""
    second = add_selectable(repo, engagement=0.2, topic=0.9)
    first = add_selectable(repo, engagement=0.8, topic=0.1)
    assert selection_ids(repo) == [first, second]


def test_selection_order_breaks_topic_ties_by_newer_published_date(repo: ItemRepository) -> None:
    """Catches: published_date key dropped; published_date ascending (oldest first)."""
    older = add_selectable(repo, published=date(2026, 10, 1))
    newer = add_selectable(repo, published=date(2026, 10, 5))
    assert selection_ids(repo) == [newer, older]


def test_selection_order_ranks_topic_before_published_date(repo: ItemRepository) -> None:
    """Catches: published_date compared before topic."""
    second = add_selectable(repo, topic=0.1, published=date(2026, 10, 9))
    first = add_selectable(repo, topic=0.9, published=date(2026, 10, 1))
    assert selection_ids(repo) == [first, second]


def test_selection_order_breaks_full_ties_by_id_ascending(
    repo: ItemRepository, db_session: Session
) -> None:
    """Catches: id key dropped; id descending.

    The query has no ORDER BY, so SQLite would hand rows back in id order and
    hide a missing id key. ``reverse_unordered_selects`` makes it return them in
    reverse, so only the key can restore id order.
    """
    ids = [add_selectable(repo) for _ in range(3)]
    db_session.execute(text("PRAGMA reverse_unordered_selects = ON"))
    raw = db_session.execute(
        select(Item.id).where(Item.status == "ENRICHED", Item.language == "EN")
    ).scalars()
    assert list(raw) == ids[::-1]  # the pragma took effect
    assert selection_ids(repo) == ids


def test_selection_order_puts_a_missing_score_last(repo: ItemRepository) -> None:
    """Catches: missing values first; a missing score treated as 0.0."""
    missing = add_selectable(repo, score=None, engagement=0.9)
    zero = add_selectable(repo, score=0.0, engagement=0.1)
    assert selection_ids(repo) == [zero, missing]


def test_selection_order_puts_a_missing_engagement_last(repo: ItemRepository) -> None:
    """Catches: missing values first; a missing signal treated as 0.0."""
    missing = add_selectable(repo, engagement=None, topic=0.9)
    zero = add_selectable(repo, engagement=0.0, topic=0.1)
    assert selection_ids(repo) == [zero, missing]


def test_selection_order_puts_a_missing_topic_last(repo: ItemRepository) -> None:
    """Catches: missing values first; a missing signal treated as 0.0."""
    missing = add_selectable(repo, topic=None, published=date(2026, 10, 9))
    zero = add_selectable(repo, topic=0.0, published=date(2026, 10, 1))
    assert selection_ids(repo) == [zero, missing]


def test_selection_order_puts_a_missing_published_date_last(repo: ItemRepository) -> None:
    """Catches: missing values first."""
    missing = add_selectable(repo, published=None)
    old = add_selectable(repo, published=date(2020, 1, 1))
    assert selection_ids(repo) == [old, missing]


@pytest.mark.parametrize(
    "breakdown",
    [
        "{not json",
        "[0.9, 0.9]",
        "null",
        '"engagement"',
        '{"engagement": "0.9", "topic": "0.9"}',
        '{"engagement": true, "topic": true}',
        '{"engagement": NaN, "topic": NaN}',
        '{"engagement": Infinity, "topic": Infinity}',
        "",
    ],
    ids=[
        "malformed",
        "array",
        "null",
        "string",
        "numeric_strings",
        "booleans",
        "nan",
        "infinity",
        "empty",
    ],
)
def test_selection_order_treats_an_unusable_breakdown_as_missing(
    repo: ItemRepository, breakdown: str
) -> None:
    """Catches: JSON errors not caught; non-objects accepted; booleans accepted;
    NaN or infinities accepted as values.

    The unusable item is inserted first and has the newer date, so if its
    breakdown were read as high signals, or ignored in favour of the date, it
    would come first. It must come after the item with real signals, and the
    query must not raise.
    """
    unusable = add_selectable(repo, breakdown=breakdown, published=date(2026, 10, 9))
    valid = add_selectable(repo, engagement=0.1, topic=0.1, published=date(2026, 10, 1))
    assert selection_ids(repo) == [valid, unusable]


def test_selection_order_applies_every_key_together(repo: ItemRepository) -> None:
    """The full D-010 order on one mixed set, read top to bottom."""
    e = add_selectable(repo, score=0.6, engagement=0.5, topic=0.5, published=date(2026, 10, 1))
    g = add_selectable(repo, score=None)
    c = add_selectable(repo, score=0.6, engagement=0.5, topic=0.9)
    a = add_selectable(repo, score=0.9)
    f = add_selectable(repo, score=0.6, engagement=None)
    b = add_selectable(repo, score=0.6, engagement=0.8)
    d = add_selectable(repo, score=0.6, engagement=0.5, topic=0.5, published=date(2026, 10, 5))
    e2 = add_selectable(repo, score=0.6, engagement=0.5, topic=0.5, published=date(2026, 10, 1))
    assert selection_ids(repo) == [a, b, c, d, e, e2, f, g]


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
