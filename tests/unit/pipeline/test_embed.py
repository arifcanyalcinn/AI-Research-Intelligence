"""
Unit tests for EmbedStage (SDS §3.3, §5.6, §5.7).

Two things these tests exist to pin above all others:

  1. **signal_breakdown is merged, not overwritten.** RankStage writes the four
     ranking signals at COLLECTED -> RANKED; this stage adds a fifth key. A
     plain `json.dumps({"novelty": d})` would destroy the four with no
     exception, no constraint violation and no failing test anywhere else in
     the suite. `test_novelty_merge_preserves_the_ranking_signals` and its
     siblings fail if the merge becomes an overwrite.

  2. **Check before add.** §5.7 requires the dedup check to precede the index
     update; the reverse makes every item its own nearest neighbour at 1.0.
     `test_the_item_is_not_in_the_index_when_it_is_checked` asserts the
     ordering directly rather than inferring it from an outcome.

The embedder and deduplicator are doubles, so this module needs neither numpy
nor usearch: the stage's own logic is what is under test, and the real
implementations are covered in tests/unit/backends/ and tests/unit/dedup/. Run
against in-memory SQLite, following tests/unit/pipeline/test_rank.py.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from arip.db.database import init_db
from arip.db.models import Item
from arip.db.repositories.items import ItemRepository
from arip.enums import ItemStatus
from arip.exceptions import EmbeddingError
from arip.pipeline.stages.embed import NOVELTY_WITHOUT_NEIGHBOUR, EmbedStage
from arip.state_machine import StateMachine

RUN_ID = 1
RANKING_SIGNALS = {
    "recency": 0.5,
    "authority": 0.85,
    "engagement": 0.25,
    "topic": 1.0,
}


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """BaseEmbedder-shaped. A vector is a plain list — no numpy needed."""

    backend_id = "fake"

    def __init__(self, error: EmbeddingError | None = None) -> None:
        self.error = error
        self.entered = 0
        self.exited = 0
        self.texts: list[str] = []

    @property
    def embedding_dim(self) -> int:
        return 4

    @property
    def model_name(self) -> str:
        return "stub:all-MiniLM-L6-v2"

    def __enter__(self) -> FakeEmbedder:
        self.entered += 1
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.exited += 1

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        if self.error is not None:
            raise self.error
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeDeduplicator:
    """SemanticDeduplicator-shaped, with a scripted verdict and a call log."""

    def __init__(
        self,
        verdicts: list[tuple[bool, int | None]] | None = None,
        similarities: list[float] | None = None,
    ) -> None:
        self.verdicts = list(verdicts or [])
        self.similarities = list(similarities or [])
        self.calls: list[tuple[str, int]] = []
        self.added: list[int] = []
        self.loaded = 0
        self.saves = 0
        self.indexed: set[int] = set()

    def load_index(self) -> None:
        self.loaded += 1

    def save_index(self) -> None:
        self.saves += 1

    def nearest(self, vector: object, item_id: int) -> tuple[int | None, float]:
        similarity = self.similarities.pop(0) if self.similarities else 0.0
        verdict = self.verdicts[0] if self.verdicts else (False, None)
        return (verdict[1], similarity)

    def is_duplicate(self, vector: object, item_id: int) -> tuple[bool, int | None]:
        self.calls.append(("is_duplicate", item_id))
        return self.verdicts.pop(0) if self.verdicts else (False, None)

    def contains(self, item_id: int) -> bool:
        return item_id in self.indexed

    def add(self, item_id: int, vector: object) -> None:
        self.calls.append(("add", item_id))
        self.added.append(item_id)
        self.indexed.add(item_id)

    @property
    def size(self) -> int:
        return len(self.indexed)


# ---------------------------------------------------------------------------
# Fixtures
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


def add_item(
    repo: ItemRepository,
    *,
    external_id: str = "x1",
    status: ItemStatus = ItemStatus.RANKED,
    title: str | None = "A Title",
    abstract: str | None = "An abstract.",
    breakdown: dict[str, float] | None = None,
    embedding_computed_at: datetime | None = None,
) -> Item:
    """Insert one item in the requested state, via the repository.

    Through ItemRepository.create() rather than a bare Item(), so the uuid and
    every other default the repository supplies are present — the same
    convention as tests/unit/pipeline/test_rank.py.
    """
    return repo.create(
        {
            "source_id": "arxiv",
            "source_type": "PAPER",
            "external_id": external_id,
            "content_hash": f"hash-{external_id}",
            "status": status.value,
            "language": "EN",
            "title": title,
            "abstract": abstract,
            "primary_url": "https://example.org/x",
            "collected_at": datetime(2026, 9, 1, 12, 0, 0),
            "importance_score": 0.6,
            "signal_breakdown": json.dumps(
                RANKING_SIGNALS if breakdown is None else breakdown
            ),
            "embedding_computed_at": embedding_computed_at,
        }
    )


@pytest.fixture
def make_stage(item_repo: ItemRepository):
    def _factory(
        embedder: FakeEmbedder | None = None,
        deduplicator: FakeDeduplicator | None = None,
    ) -> tuple[EmbedStage, FakeEmbedder, FakeDeduplicator]:
        the_embedder = embedder or FakeEmbedder()
        the_dedup = deduplicator or FakeDeduplicator()
        stage = EmbedStage(
            item_repo=item_repo,
            state_machine=StateMachine(item_repo),
            embedder=the_embedder,  # type: ignore[arg-type]
            deduplicator_factory=lambda embedding_dim: the_dedup,  # type: ignore[arg-type,misc]
        )
        return stage, the_embedder, the_dedup

    return _factory


def breakdown_of(session: Session, item_id: int) -> dict[str, float]:
    session.expire_all()
    return json.loads(session.get(Item, item_id).signal_breakdown)


# ---------------------------------------------------------------------------
# signal_breakdown READ-MERGE-WRITE — the defect this batch has warned about
# ---------------------------------------------------------------------------


def test_novelty_merge_preserves_the_ranking_signals(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """The four signals RankStage wrote must survive the embedding stage.

    Replace `_merged_breakdown` with a plain overwrite and this fails: the four
    keys disappear, no exception is raised, and nothing else in the suite
    notices.
    """
    item = add_item(item_repo)
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    merged = breakdown_of(db_session, item.id)
    for key, value in RANKING_SIGNALS.items():
        assert merged[key] == value
    assert "novelty" in merged
    assert len(merged) == len(RANKING_SIGNALS) + 1


def test_novelty_is_added_not_substituted(db_session: Session, item_repo: ItemRepository, make_stage) -> None:
    """An explicit guard against `{"novelty": d}` replacing the object."""
    item = add_item(item_repo, breakdown={"recency": 0.11, "authority": 0.22})
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    merged = breakdown_of(db_session, item.id)
    assert merged["recency"] == 0.11
    assert merged["authority"] == 0.22


def test_novelty_is_the_ann_distance(db_session: Session, item_repo: ItemRepository, make_stage) -> None:
    """§5.5: "the ANN distance to nearest neighbor" — distance, not similarity."""
    item = add_item(item_repo)
    dedup = FakeDeduplicator(verdicts=[(False, 7)], similarities=[0.25])
    stage, _, _ = make_stage(deduplicator=dedup)

    stage.run(RUN_ID)

    assert breakdown_of(db_session, item.id)["novelty"] == pytest.approx(0.75)


def test_novelty_without_a_neighbour_is_recorded(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """An empty index gives no neighbour; the key is still written."""
    item = add_item(item_repo)
    stage, _, _ = make_stage(deduplicator=FakeDeduplicator(verdicts=[(False, None)]))

    stage.run(RUN_ID)

    assert breakdown_of(db_session, item.id)["novelty"] == NOVELTY_WITHOUT_NEIGHBOUR


def test_a_missing_breakdown_does_not_stop_the_item(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """NULL signal_breakdown (a FILTERED-then-requeued item) must still embed."""
    item = add_item(item_repo)
    item.signal_breakdown = None
    db_session.commit()
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    db_session.expire_all()
    assert db_session.get(Item, item.id).status == ItemStatus.ENRICHED.value
    assert breakdown_of(db_session, item.id) == {
        "novelty": NOVELTY_WITHOUT_NEIGHBOUR
    }


def test_an_unparseable_breakdown_does_not_stop_the_item(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """The column is diagnostic (§4). Losing it beats failing a good embedding."""
    item = add_item(item_repo)
    item.signal_breakdown = "{not json"
    db_session.commit()
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    db_session.expire_all()
    assert db_session.get(Item, item.id).status == ItemStatus.ENRICHED.value
    assert "novelty" in breakdown_of(db_session, item.id)


def test_a_non_object_breakdown_does_not_stop_the_item(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """Valid JSON that is not an object — e.g. a bare number — is replaced."""
    item = add_item(item_repo)
    item.signal_breakdown = "42"
    db_session.commit()
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    assert breakdown_of(db_session, item.id) == {
        "novelty": NOVELTY_WITHOUT_NEIGHBOUR
    }


def test_importance_score_is_not_touched(db_session: Session, item_repo: ItemRepository, make_stage) -> None:
    """AD-19: novelty is recorded, never scored."""
    item = add_item(item_repo)
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    db_session.expire_all()
    assert db_session.get(Item, item.id).importance_score == 0.6


# ---------------------------------------------------------------------------
# Ordering — check before add (SDS §5.7 over §3.3)
# ---------------------------------------------------------------------------


def test_the_item_is_not_in_the_index_when_it_is_checked(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """`is_duplicate` must be called before `add`, for the same item.

    Reverse the two and every item is its own nearest neighbour at 1.0, so the
    whole run is marked DUPLICATE — terminal, and silent.
    """
    item = add_item(item_repo)
    stage, _, dedup = make_stage()

    stage.run(RUN_ID)

    assert dedup.calls == [("is_duplicate", item.id), ("add", item.id)]


def test_a_duplicate_is_never_added_to_the_index(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """§5.7's reconciliation excludes DUPLICATE, so an indexed one is unrepairable."""
    survivor = add_item(item_repo, external_id="s", status=ItemStatus.ENRICHED)
    add_item(item_repo, external_id="d")
    stage, _, dedup = make_stage(
        deduplicator=FakeDeduplicator(verdicts=[(True, survivor.id)])
    )

    stage.run(RUN_ID)

    assert dedup.added == []


# ---------------------------------------------------------------------------
# Transitions — SDS §3.3
# ---------------------------------------------------------------------------


def test_a_passing_item_reaches_enriched(db_session: Session, item_repo: ItemRepository, make_stage) -> None:
    item = add_item(item_repo)
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    db_session.expire_all()
    stored = db_session.get(Item, item.id)
    assert stored.status == ItemStatus.ENRICHED.value
    assert stored.enriched_at is not None


def test_embedding_metadata_is_written(db_session: Session, item_repo: ItemRepository, make_stage) -> None:
    """§3.3 RANKED -> EMBEDDED sets both columns; EMBEDDED has no auto-timestamp."""
    item = add_item(item_repo)
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    db_session.expire_all()
    stored = db_session.get(Item, item.id)
    assert stored.embedding_computed_at is not None
    assert stored.embedding_model_name == "stub:all-MiniLM-L6-v2"


def test_the_model_name_comes_from_the_backend_not_the_config(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """The Stage 2 defect, guarded at its consumer.

    Reading `config.embeddings.model_name` here would record stub vectors as
    `all-MiniLM-L6-v2`, which §5.7's reconciliation query cannot detect.
    """
    item = add_item(item_repo)
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    db_session.expire_all()
    assert db_session.get(Item, item.id).embedding_model_name.startswith("stub:")


def test_a_duplicate_reaches_duplicate_with_its_survivor(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """§3.3 EMBEDDED -> DUPLICATE sets duplicate_of_id and is_semantic_duplicate."""
    survivor = add_item(item_repo, external_id="s", status=ItemStatus.ENRICHED)
    dup = add_item(item_repo, external_id="d")
    stage, _, _ = make_stage(
        deduplicator=FakeDeduplicator(verdicts=[(True, survivor.id)])
    )

    stage.run(RUN_ID)

    db_session.expire_all()
    stored = db_session.get(Item, dup.id)
    assert stored.status == ItemStatus.DUPLICATE.value
    assert stored.duplicate_of_id == survivor.id
    assert stored.is_semantic_duplicate is True


def test_a_duplicate_still_records_its_embedding(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """It passed through EMBEDDED, so the columns §3.3 sets there are written."""
    survivor = add_item(item_repo, external_id="s", status=ItemStatus.ENRICHED)
    dup = add_item(item_repo, external_id="d")
    stage, _, _ = make_stage(
        deduplicator=FakeDeduplicator(verdicts=[(True, survivor.id)])
    )

    stage.run(RUN_ID)

    db_session.expire_all()
    assert db_session.get(Item, dup.id).embedding_computed_at is not None


def test_an_embedding_error_fails_the_item(db_session: Session, item_repo: ItemRepository, make_stage) -> None:
    """§3.3 RANKED -> FAILED, embed_error."""
    item = add_item(item_repo)
    stage, _, _ = make_stage(embedder=FakeEmbedder(error=EmbeddingError("no model")))

    stage.run(RUN_ID)

    db_session.expire_all()
    stored = db_session.get(Item, item.id)
    assert stored.status == ItemStatus.FAILED.value
    assert stored.failed_at_stage == "EMBEDDING"
    assert "no model" in stored.failure_reason


def test_a_failed_item_is_not_indexed(db_session: Session, item_repo: ItemRepository, make_stage) -> None:
    item = add_item(item_repo)
    stage, _, dedup = make_stage(
        embedder=FakeEmbedder(error=EmbeddingError("boom"))
    )

    stage.run(RUN_ID)

    assert dedup.added == []
    assert item.id not in dedup.indexed


def test_only_ranked_items_are_processed(db_session: Session, item_repo: ItemRepository, make_stage) -> None:
    """COLLECTED, FILTERED and ENRICHED items are not this stage's work."""
    ranked = add_item(item_repo, external_id="r")
    collected = add_item(item_repo, external_id="c", status=ItemStatus.COLLECTED)
    filtered = add_item(item_repo, external_id="f", status=ItemStatus.FILTERED)
    stage, _, _ = make_stage()

    stage.run(RUN_ID)

    db_session.expire_all()
    assert db_session.get(Item, ranked.id).status == ItemStatus.ENRICHED.value
    assert db_session.get(Item, collected.id).status == ItemStatus.COLLECTED.value
    assert db_session.get(Item, filtered.id).status == ItemStatus.FILTERED.value


# ---------------------------------------------------------------------------
# The embedded text — SDS §5.6
# ---------------------------------------------------------------------------


def test_embedding_text_is_title_then_abstract(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """§5.6: `item.title + ". " + (item.abstract or "")`."""
    add_item(item_repo, title="Attention Is All You Need", abstract="We propose.")
    stage, embedder, _ = make_stage()

    stage.run(RUN_ID)

    assert embedder.texts == ["Attention Is All You Need. We propose."]


def test_embedding_text_without_an_abstract(db_session: Session, item_repo: ItemRepository, make_stage) -> None:
    """TD-003: HF Models and Spaces have abstract=None, so the title carries it."""
    add_item(item_repo, title="org/model-name", abstract=None)
    stage, embedder, _ = make_stage()

    stage.run(RUN_ID)

    assert embedder.texts == ["org/model-name. "]


def test_embedding_text_with_a_null_title_does_not_raise(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """items.title is nullable in §4, so the SDS expression needs `or ""`."""
    item = add_item(item_repo, title=None, abstract="Only an abstract.")
    stage, embedder, _ = make_stage()

    stage.run(RUN_ID)

    assert embedder.texts == [". Only an abstract."]
    db_session.expire_all()
    assert db_session.get(Item, item.id).status == ItemStatus.ENRICHED.value


# ---------------------------------------------------------------------------
# Lifecycle and the empty batch
# ---------------------------------------------------------------------------


def test_the_model_is_loaded_once_for_the_pass(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """§5.6: load once, keep in memory for the pass, unload after."""
    add_item(item_repo, external_id="a")
    add_item(item_repo, external_id="b")
    stage, embedder, _ = make_stage()

    stage.run(RUN_ID)

    assert (embedder.entered, embedder.exited) == (1, 1)


def test_an_empty_batch_loads_no_model_and_returns_none(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """Nothing RANKED: no model, no index, and nothing for the caller to save."""
    stage, embedder, dedup = make_stage()

    result = stage.run(RUN_ID)

    assert result is None
    assert embedder.entered == 0
    assert dedup.loaded == 0


def test_run_returns_the_deduplicator_for_the_caller_to_save(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """The stage never saves the index itself — the DB must commit first."""
    add_item(item_repo)
    stage, _, dedup = make_stage()

    result = stage.run(RUN_ID)

    assert result is dedup
    assert dedup.saves == 0, "saving is the orchestrator's job, after the commit"


def test_the_index_is_loaded_before_any_item_is_checked(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    add_item(item_repo)
    stage, _, dedup = make_stage()

    stage.run(RUN_ID)

    assert dedup.loaded == 1


# ---------------------------------------------------------------------------
# Reconciliation — SDS §5.7
# ---------------------------------------------------------------------------


def test_reconciliation_restores_an_item_missing_from_the_index(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """§5.7: an embedded, non-terminal item absent from the index is re-added."""
    lost = add_item(
        item_repo,
        external_id="lost",
        status=ItemStatus.ENRICHED,
        embedding_computed_at=datetime(2026, 9, 1, 9, 0, 0),
    )
    add_item(item_repo, external_id="new")
    stage, _, dedup = make_stage()

    stage.run(RUN_ID)

    assert lost.id in dedup.added


def test_reconciliation_skips_items_already_in_the_index(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    present = add_item(
        item_repo,
        external_id="present",
        status=ItemStatus.ENRICHED,
        embedding_computed_at=datetime(2026, 9, 1, 9, 0, 0),
    )
    add_item(item_repo, external_id="new")
    dedup = FakeDeduplicator()
    dedup.indexed.add(present.id)
    stage, _, _ = make_stage(deduplicator=dedup)

    stage.run(RUN_ID)

    assert present.id not in dedup.added


def test_reconciliation_ignores_terminal_items(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """The §5.7 query excludes DUPLICATE, FILTERED and FAILED.

    Used as ItemRepository.get_all_with_embeddings() already implements it —
    this stage adds no filtering of its own.
    """
    for external_id, status in (
        ("dup", ItemStatus.DUPLICATE),
        ("filt", ItemStatus.FILTERED),
        ("fail", ItemStatus.FAILED),
    ):
        add_item(
            item_repo,
            external_id=external_id,
            status=status,
            embedding_computed_at=datetime(2026, 9, 1, 9, 0, 0),
        )
    new = add_item(item_repo, external_id="new")
    stage, _, dedup = make_stage()

    stage.run(RUN_ID)

    assert dedup.added == [new.id]


def test_reconciliation_runs_before_the_new_items(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """A restored neighbour has to be present before anything is checked against it."""
    lost = add_item(
        item_repo,
        external_id="lost",
        status=ItemStatus.ENRICHED,
        embedding_computed_at=datetime(2026, 9, 1, 9, 0, 0),
    )
    fresh = add_item(item_repo, external_id="new")
    stage, _, dedup = make_stage()

    stage.run(RUN_ID)

    assert dedup.calls[0] == ("add", lost.id)
    assert ("is_duplicate", fresh.id) in dedup.calls
    assert dedup.calls.index(("add", lost.id)) < dedup.calls.index(
        ("is_duplicate", fresh.id)
    )


def test_a_reconciliation_failure_does_not_abort_the_run(
    db_session: Session, item_repo: ItemRepository, make_stage
) -> None:
    """One un-embeddable item must not stop the stage doing new work.

    The embedder here raises for every call, so reconciliation fails and the new
    item then fails too — but the run reaches the end and both are accounted
    for, rather than the exception escaping the stage.
    """
    add_item(
        item_repo,
        external_id="lost",
        status=ItemStatus.ENRICHED,
        embedding_computed_at=datetime(2026, 9, 1, 9, 0, 0),
    )
    fresh = add_item(item_repo, external_id="new")
    stage, _, dedup = make_stage(
        embedder=FakeEmbedder(error=EmbeddingError("model gone"))
    )

    result = stage.run(RUN_ID)

    assert result is dedup
    db_session.expire_all()
    assert db_session.get(Item, fresh.id).status == ItemStatus.FAILED.value
