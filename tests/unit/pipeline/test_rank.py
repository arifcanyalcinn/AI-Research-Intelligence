"""
Unit tests for RankStage.

Covers the Phase 3 gate items that Batch 8 makes evaluable:

  - items receive non-trivial importance scores reflecting real signals
  - low-score items are filtered before generation, verifiable in the database

and the transition ownership fixed by SDS §8.5:

  - RankStage owns COLLECTED -> RANKED and COLLECTED -> FILTERED
  - CollectStage owns COLLECTED -> FAILED and stamps normalized_at

Tested against an in-memory SQLite database, following the convention in
tests/unit/pipeline/test_collect.py. No network. Scoring arithmetic itself is
covered by tests/unit/test_scorer.py; these tests assert what the stage does
with a score, not how the score is computed.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from arip.config import RankingSettings, SignalWeights
from arip.db.database import init_db
from arip.db.models import Item
from arip.db.repositories.items import ItemRepository
from arip.enums import ItemStatus
from arip.pipeline.stages.rank import RankStage
from arip.ranking.scorer import Scorer
from arip.state_machine import StateMachine

RUN_ID = 1


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def db_session() -> Session:
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


def ranking(**overrides: object) -> RankingSettings:
    base: dict[str, object] = {
        "min_score": 0.35,
        "recency_k": 0.15,
        "weights": SignalWeights(),
        "topic_keywords": [],
        "source_authority": {
            "arxiv": 0.85,
            "huggingface_papers": 0.80,
            "github_trending": 0.65,
            "huggingface_models": 0.55,
            "huggingface_spaces": 0.45,
        },
    }
    base.update(overrides)
    return RankingSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def make_stage(item_repo: ItemRepository):
    """Return a factory building a RankStage over the given ranking config."""

    def _factory(config: RankingSettings | None = None) -> RankStage:
        settings = config or ranking()
        return RankStage(
            item_repo=item_repo,
            scorer=Scorer(settings),
            state_machine=StateMachine(item_repo),
            min_score=settings.min_score,
        )

    return _factory


def add_item(
    repo: ItemRepository,
    *,
    source_id: str = "github_trending",
    source_type: str = "REPO",
    status: ItemStatus = ItemStatus.COLLECTED,
    published_date: date | None = None,
    title: str = "A Repository",
    source_signals: dict | None = None,
    external_id: str | None = None,
) -> Item:
    """Insert one item in the requested state."""
    suffix = external_id or f"{source_id}-{title}-{id(title)}"
    return repo.create(
        {
            "source_id": source_id,
            "source_type": source_type,
            "external_id": suffix,
            "content_hash": f"hash-{suffix}",
            "status": status.value,
            "language": "EN",
            "title": title,
            "primary_url": "https://example.org/x",
            "published_date": published_date,
            "source_signals": json.dumps(source_signals) if source_signals else None,
        }
    )


def statuses(session: Session) -> dict[str, str]:
    return {i.external_id: i.status for i in session.query(Item).all()}


# ---------------------------------------------------------------------------
# Threshold decision — SDS §3.3, §3.5
# ---------------------------------------------------------------------------


def test_high_scoring_item_is_ranked(make_stage, item_repo, db_session) -> None:
    """An item at or above min_score moves to RANKED."""
    add_item(
        item_repo,
        published_date=date.today(),
        source_signals={"stars": 500},
        external_id="strong",
    )

    make_stage().run(RUN_ID)

    assert statuses(db_session)["strong"] == ItemStatus.RANKED.value


def test_low_scoring_item_is_filtered(make_stage, item_repo, db_session) -> None:
    """An item below min_score moves to FILTERED (Phase 3 gate item)."""
    add_item(
        item_repo,
        source_id="huggingface_spaces",
        source_type="SPACE",
        published_date=None,
        source_signals=None,
        external_id="weak",
    )

    make_stage().run(RUN_ID)

    assert statuses(db_session)["weak"] == ItemStatus.FILTERED.value


def test_item_exactly_at_the_threshold_is_ranked(item_repo, db_session) -> None:
    """The §3.3 guard is `>=`, so the boundary passes rather than fails."""
    add_item(item_repo, external_id="boundary")
    # Authority alone: 0.65 * 1.0 == 0.65, with min_score set to match exactly.
    config = ranking(
        min_score=0.65,
        weights=SignalWeights(recency=0.0, authority=1.0, engagement=0.0, topic=0.0),
    )
    stage = RankStage(
        item_repo=item_repo,
        scorer=Scorer(config),
        state_machine=StateMachine(item_repo),
        min_score=config.min_score,
    )

    stage.run(RUN_ID)

    assert statuses(db_session)["boundary"] == ItemStatus.RANKED.value


def test_threshold_is_read_from_configuration(item_repo, db_session) -> None:
    """The same item ranks or filters depending on min_score alone."""
    add_item(item_repo, published_date=date.today(), external_id="middling")

    permissive = RankingSettings(**{**ranking().model_dump(), "min_score": 0.01})
    stage = RankStage(
        item_repo=item_repo,
        scorer=Scorer(permissive),
        state_machine=StateMachine(item_repo),
        min_score=permissive.min_score,
    )
    stage.run(RUN_ID)

    assert statuses(db_session)["middling"] == ItemStatus.RANKED.value


def test_strict_threshold_filters_everything(make_stage, item_repo, db_session) -> None:
    """A threshold above the maximum possible score filters every item."""
    add_item(item_repo, published_date=date.today(), source_signals={"stars": 9}, external_id="a")
    add_item(item_repo, published_date=date.today(), source_signals={"stars": 1}, external_id="b")

    make_stage(ranking(min_score=1.01)).run(RUN_ID)

    assert set(statuses(db_session).values()) == {ItemStatus.FILTERED.value}


# ---------------------------------------------------------------------------
# Persisted fields — SDS §3.3, §3.5, §4.2
# ---------------------------------------------------------------------------


def test_ranked_item_stores_the_score(make_stage, item_repo, db_session) -> None:
    """importance_score is persisted for a ranked item (§3.3)."""
    item = add_item(item_repo, published_date=date.today(), source_signals={"stars": 5})

    make_stage().run(RUN_ID)

    stored = db_session.get(Item, item.id)
    assert stored.importance_score is not None
    assert 0.0 <= stored.importance_score <= 1.0


def test_ranked_item_stores_the_signal_breakdown(make_stage, item_repo, db_session) -> None:
    """signal_breakdown is persisted as JSON with the four signal keys (§3.3, §4.2)."""
    item = add_item(item_repo, published_date=date.today(), source_signals={"stars": 5})

    make_stage().run(RUN_ID)

    breakdown = json.loads(db_session.get(Item, item.id).signal_breakdown)
    assert set(breakdown) == {"recency", "authority", "engagement", "topic"}


def test_stored_breakdown_reproduces_the_stored_score(
    make_stage, item_repo, db_session
) -> None:
    """The persisted breakdown multiplies out to the persisted score."""
    item = add_item(item_repo, published_date=date.today(), source_signals={"stars": 5})

    make_stage().run(RUN_ID)

    stored = db_session.get(Item, item.id)
    breakdown = json.loads(stored.signal_breakdown)
    weights = SignalWeights()
    expected = (
        weights.recency * breakdown["recency"]
        + weights.authority * breakdown["authority"]
        + weights.engagement * breakdown["engagement"]
        + weights.topic * breakdown["topic"]
    )
    assert stored.importance_score == pytest.approx(expected)


def test_filtered_item_stores_the_score(make_stage, item_repo, db_session) -> None:
    """importance_score is persisted for a filtered item too (§3.5)."""
    item = add_item(item_repo, source_id="huggingface_spaces", source_type="SPACE")

    make_stage(ranking(min_score=1.01)).run(RUN_ID)

    stored = db_session.get(Item, item.id)
    assert stored.status == ItemStatus.FILTERED.value
    assert stored.importance_score is not None


def test_filtered_item_stores_no_signal_breakdown(
    make_stage, item_repo, db_session
) -> None:
    """§3.5 narrows the FILTERED action to importance_score alone.

    The breakdown is logged rather than stored. Storing it would go beyond the
    action line we amended, so this test pins the narrower behaviour.
    """
    item = add_item(item_repo, published_date=date.today())

    make_stage(ranking(min_score=1.01)).run(RUN_ID)

    assert db_session.get(Item, item.id).signal_breakdown is None


def test_no_filtered_reason_is_written(make_stage, item_repo, db_session) -> None:
    """filtered_reason is not a column (§3.5, TD-011 principle).

    ItemRepository.update_status() discards unknown fields with a warning, so
    passing one would fail silently. Guards against a future change adding it
    back without a schema migration.
    """
    item = add_item(item_repo, published_date=date.today())

    make_stage(ranking(min_score=1.01)).run(RUN_ID)

    assert not hasattr(db_session.get(Item, item.id), "filtered_reason")


def test_ranked_at_is_stamped(make_stage, item_repo, db_session) -> None:
    """The state machine stamps ranked_at on the RANKED transition."""
    item = add_item(item_repo, published_date=date.today(), source_signals={"stars": 5})

    make_stage().run(RUN_ID)

    assert isinstance(db_session.get(Item, item.id).ranked_at, datetime)


def test_scores_differ_across_items(make_stage, item_repo, db_session) -> None:
    """Scores reflect real signals rather than a constant (Phase 3 gate item).

    This is the assertion that a stub RankStage returning 1.0 could not make.
    """
    add_item(item_repo, published_date=date.today(), source_signals={"stars": 900},
             external_id="popular")
    add_item(item_repo, published_date=date(2020, 1, 1), source_signals={"stars": 1},
             external_id="obscure")

    make_stage(ranking(min_score=0.0)).run(RUN_ID)

    scores = {i.external_id: i.importance_score for i in db_session.query(Item).all()}
    assert scores["popular"] > scores["obscure"]


# ---------------------------------------------------------------------------
# Item selection — SDS §8.5
# ---------------------------------------------------------------------------


def test_only_collected_items_are_processed(make_stage, item_repo, db_session) -> None:
    """Items in other states are untouched."""
    add_item(item_repo, status=ItemStatus.COLLECTED, published_date=date.today(),
             external_id="fresh")
    add_item(item_repo, status=ItemStatus.FAILED, external_id="broken")
    add_item(item_repo, status=ItemStatus.ARCHIVED, external_id="done")

    make_stage().run(RUN_ID)

    result = statuses(db_session)
    assert result["fresh"] != ItemStatus.COLLECTED.value
    assert result["broken"] == ItemStatus.FAILED.value
    assert result["done"] == ItemStatus.ARCHIVED.value


def test_failed_items_are_left_alone(make_stage, item_repo, db_session) -> None:
    """CollectStage owns COLLECTED -> FAILED; RankStage must not revisit it (§8.5)."""
    item = add_item(item_repo, status=ItemStatus.FAILED, external_id="norm-failure")
    item.failed_at_stage = "NORMALIZATION"
    db_session.flush()

    make_stage().run(RUN_ID)

    stored = db_session.get(Item, item.id)
    assert stored.status == ItemStatus.FAILED.value
    assert stored.importance_score is None


def test_empty_batch_is_a_no_op(make_stage, db_session) -> None:
    """A run with nothing collected is not an error."""
    make_stage().run(RUN_ID)

    assert db_session.query(Item).count() == 0


def test_second_run_does_not_rescore_ranked_items(
    make_stage, item_repo, db_session
) -> None:
    """Once out of COLLECTED, an item is no longer this stage's business."""
    item = add_item(item_repo, published_date=date.today(), source_signals={"stars": 5})
    stage = make_stage()

    stage.run(RUN_ID)
    first_score = db_session.get(Item, item.id).importance_score
    stage.run(RUN_ID + 1)

    stored = db_session.get(Item, item.id)
    assert stored.status == ItemStatus.RANKED.value
    assert stored.importance_score == first_score


def test_items_collected_on_an_earlier_run_are_ranked(
    make_stage, item_repo, db_session
) -> None:
    """Every COLLECTED item forms the batch, not only this run's arrivals."""
    add_item(item_repo, published_date=date.today(), external_id="older")
    add_item(item_repo, published_date=date.today(), external_id="newer")

    make_stage(ranking(min_score=0.0)).run(RUN_ID)

    assert set(statuses(db_session).values()) == {ItemStatus.RANKED.value}


# ---------------------------------------------------------------------------
# Batch scoping — SDS §5.5
# ---------------------------------------------------------------------------


def test_engagement_is_relative_to_the_other_items_in_the_run(
    make_stage, item_repo, db_session
) -> None:
    """The percentile is computed across the batch, so peers change the score."""
    add_item(item_repo, source_signals={"stars": 50}, published_date=date.today(),
             external_id="lonely")

    make_stage(ranking(min_score=0.0)).run(RUN_ID)
    alone = db_session.query(Item).filter(Item.external_id == "lonely").one()
    alone_score = alone.importance_score

    add_item(item_repo, source_signals={"stars": 50}, published_date=date.today(),
             external_id="peer_a")
    add_item(item_repo, source_signals={"stars": 5000}, published_date=date.today(),
             external_id="peer_b")
    make_stage(ranking(min_score=0.0)).run(RUN_ID + 1)

    peer_a = db_session.query(Item).filter(Item.external_id == "peer_a").one()
    # Alone in its cohort the first item scored engagement 1.0; with a stronger
    # peer present, an identical item scores 0.0.
    assert alone_score > peer_a.importance_score


# ---------------------------------------------------------------------------
# Session ownership — SDS §5.14
# ---------------------------------------------------------------------------


def test_stage_does_not_commit_the_session(make_stage, item_repo, db_session) -> None:
    """The stage flushes but never commits — the orchestrator owns the transaction."""
    add_item(item_repo, published_date=date.today())

    make_stage().run(RUN_ID)

    assert db_session.in_transaction()


# ---------------------------------------------------------------------------
# normalized_at — SDS §8.5, ruling 6
# ---------------------------------------------------------------------------


def test_ranking_does_not_stamp_normalized_at(make_stage, item_repo, db_session) -> None:
    """§8.5: CollectStage owns normalized_at, not the RANKED transition.

    Items inserted directly here never passed through CollectStage, so the
    column stays NULL — proving RankStage does not write it, contrary to
    §3.3's original action line.
    """
    item = add_item(item_repo, published_date=date.today(), source_signals={"stars": 5})

    make_stage().run(RUN_ID)

    stored = db_session.get(Item, item.id)
    assert stored.status == ItemStatus.RANKED.value
    assert stored.normalized_at is None
