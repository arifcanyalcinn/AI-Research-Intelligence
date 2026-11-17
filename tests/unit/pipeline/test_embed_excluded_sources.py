"""
Unit tests for SDS §5.7.1 in EmbedStage: sources excluded from semantic dedup.

An item of ``huggingface_models`` or ``huggingface_spaces`` is embedded and gets
its novelty like any other item, but it is never checked for duplication and
never added to the ANN index; reconciliation leaves such items out and removes
any that an older index still holds.

The deduplicator double here computes cosine similarity for real (over plain
lists) instead of returning scripted verdicts, so "identical vector, yet not a
duplicate" is an outcome of the rule, not of the script. Like test_embed.py,
this module needs neither numpy nor usearch; the real index is exercised in
tests/unit/dedup/test_semantic_excluded.py.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from arip.db.database import init_db
from arip.db.models import Item
from arip.db.repositories.items import ItemRepository
from arip.dedup.semantic import SEMANTIC_DEDUP_EXCLUDED_SOURCES
from arip.enums import ItemStatus
from arip.pipeline.stages.embed import EmbedStage
from arip.state_machine import StateMachine

RUN_ID = 1
THRESHOLD = 0.92
RANKING_SIGNALS = {"recency": 0.5, "authority": 0.85, "engagement": 0.25, "topic": 1.0}
SOURCE_TYPE = {
    "arxiv": "PAPER",
    "huggingface_papers": "PAPER",
    "github_trending": "REPO",
    "huggingface_models": "MODEL",
    "huggingface_spaces": "SPACE",
}

SAME = [1.0, 0.0, 0.0]
"""Every test item embeds to this vector unless its title says otherwise."""


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class TitleEmbedder:
    """Embeds a text to the vector registered for its title; SAME by default."""

    backend_id = "fake"
    embedding_dim = 3
    model_name = "fake-model"

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}
        self.texts: list[str] = []

    def __enter__(self) -> TitleEmbedder:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [self.vectors.get(text.split(". ")[0], SAME) for text in texts]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class CosineDeduplicator:
    """SemanticDeduplicator-shaped, with real nearest-neighbour arithmetic."""

    def __init__(self) -> None:
        self.index: dict[int, list[float]] = {}
        self.calls: list[tuple[str, int]] = []

    def load_index(self) -> None:
        pass

    def save_index(self) -> None:
        pass

    def nearest(self, vector: list[float], item_id: int) -> tuple[int | None, float]:
        others = [(cosine(vector, v), k) for k, v in self.index.items() if k != item_id]
        if not others:
            return (None, 0.0)
        similarity, key = max(others)
        return (key, similarity)

    def is_duplicate(self, vector: list[float], item_id: int) -> tuple[bool, int | None]:
        self.calls.append(("is_duplicate", item_id))
        key, similarity = self.nearest(vector, item_id)
        if key is None:
            return (False, None)
        return (similarity >= THRESHOLD, key)

    def contains(self, item_id: int) -> bool:
        return item_id in self.index

    def add(self, item_id: int, vector: list[float]) -> None:
        self.calls.append(("add", item_id))
        self.index.setdefault(item_id, list(vector))

    def remove(self, item_id: int) -> bool:
        self.calls.append(("remove", item_id))
        return self.index.pop(item_id, None) is not None

    @property
    def size(self) -> int:
        return len(self.index)


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
    source_id: str,
    *,
    title: str,
    minute: int,
    status: ItemStatus = ItemStatus.RANKED,
    source_type: str | None = None,
    embedded: bool = False,
) -> Item:
    """One item; ``minute`` sets collected_at, and so the processing order."""
    return repo.create(
        {
            "source_id": source_id,
            "source_type": source_type or SOURCE_TYPE[source_id],
            "external_id": f"ext-{title}",
            "content_hash": f"hash-{title}",
            "status": status.value,
            "language": "EN",
            "title": title,
            "abstract": None,
            "primary_url": f"https://example.org/{title}",
            "collected_at": datetime(2026, 11, 1, 9, minute, 0),
            "importance_score": 0.6,
            "signal_breakdown": json.dumps(RANKING_SIGNALS),
            "embedding_computed_at": datetime(2026, 10, 1, 9, 0, 0) if embedded else None,
        }
    )


def run_stage(
    repo: ItemRepository,
    dedup: CosineDeduplicator | None = None,
    embedder: TitleEmbedder | None = None,
) -> tuple[CosineDeduplicator, TitleEmbedder]:
    the_dedup = dedup or CosineDeduplicator()
    the_embedder = embedder or TitleEmbedder()
    stage = EmbedStage(
        item_repo=repo,
        state_machine=StateMachine(repo),
        embedder=the_embedder,  # type: ignore[arg-type]
        deduplicator_factory=lambda embedding_dim: the_dedup,  # type: ignore[arg-type,misc]
    )
    stage.run(RUN_ID)
    return the_dedup, the_embedder


def status_of(session: Session, item: Item) -> str:
    session.expire_all()
    return session.get(Item, item.id).status


# ---------------------------------------------------------------------------
# The decision guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_id", ["huggingface_models", "huggingface_spaces"])
def test_an_excluded_source_is_never_a_semantic_duplicate(
    db_session: Session, item_repo: ItemRepository, source_id: str
) -> None:
    """T1/T2: an identical vector to an indexed item, yet ENRICHED, unchecked, unindexed."""
    survivor = add_item(item_repo, "arxiv", title="paper", minute=0)
    excluded = add_item(item_repo, source_id, title="org/model", minute=1)

    dedup, _ = run_stage(item_repo)

    assert status_of(db_session, survivor) == ItemStatus.ENRICHED.value
    assert status_of(db_session, excluded) == ItemStatus.ENRICHED.value
    assert db_session.get(Item, excluded.id).duplicate_of_id is None
    assert ("is_duplicate", excluded.id) not in dedup.calls
    assert ("add", excluded.id) not in dedup.calls
    assert not dedup.contains(excluded.id)


def test_an_excluded_item_is_never_the_survivor_of_another(
    db_session: Session, item_repo: ItemRepository
) -> None:
    """T3: a GitHub item identical to an earlier model passes, because the model is not indexed."""
    model = add_item(item_repo, "huggingface_models", title="org/model", minute=0)
    repo_item = add_item(item_repo, "github_trending", title="org/repo", minute=1)

    dedup, _ = run_stage(item_repo)

    assert status_of(db_session, model) == ItemStatus.ENRICHED.value
    assert status_of(db_session, repo_item) == ItemStatus.ENRICHED.value
    assert dedup.index == {repo_item.id: SAME}


def test_papers_are_still_deduplicated_across_sources(
    db_session: Session, item_repo: ItemRepository
) -> None:
    """T4: huggingface_papers is not excluded; an identical arxiv item still wins."""
    paper = add_item(item_repo, "arxiv", title="paper", minute=0)
    copy = add_item(item_repo, "huggingface_papers", title="paper-copy", minute=1)

    run_stage(item_repo)

    assert status_of(db_session, paper) == ItemStatus.ENRICHED.value
    db_session.expire_all()
    stored = db_session.get(Item, copy.id)
    assert stored.status == ItemStatus.DUPLICATE.value
    assert stored.duplicate_of_id == paper.id
    assert stored.is_semantic_duplicate is True


def test_an_excluded_item_gets_its_novelty_from_the_nearest_indexed_item(
    db_session: Session, item_repo: ItemRepository
) -> None:
    """T5: §5.7.1 point 4 — novelty is 1 - similarity to the nearest indexed item."""
    add_item(item_repo, "arxiv", title="paper", minute=0)
    model = add_item(item_repo, "huggingface_models", title="org/model", minute=1)
    embedder = TitleEmbedder({"org/model": [0.6, 0.8, 0.0]})  # cosine 0.6 to SAME

    run_stage(item_repo, embedder=embedder)

    db_session.expire_all()
    breakdown = json.loads(db_session.get(Item, model.id).signal_breakdown)
    assert breakdown["novelty"] == pytest.approx(0.4)
    for key, value in RANKING_SIGNALS.items():
        assert breakdown[key] == value
    assert db_session.get(Item, model.id).embedding_model_name == "fake-model"


def test_the_excluded_sources_are_exactly_models_and_spaces() -> None:
    """T6: the table of §5.7.1."""
    assert frozenset({"huggingface_models", "huggingface_spaces"}) == (
        SEMANTIC_DEDUP_EXCLUDED_SOURCES
    )


def test_the_rule_is_keyed_by_source_id_not_source_type(
    db_session: Session, item_repo: ItemRepository
) -> None:
    """T7: a github_trending item typed MODEL is still deduplicated."""
    survivor = add_item(item_repo, "github_trending", title="org/repo", minute=0)
    odd = add_item(item_repo, "github_trending", title="org/odd", minute=1, source_type="MODEL")

    dedup, _ = run_stage(item_repo)

    db_session.expire_all()
    assert ("is_duplicate", odd.id) in dedup.calls
    assert db_session.get(Item, odd.id).status == ItemStatus.DUPLICATE.value
    assert db_session.get(Item, odd.id).duplicate_of_id == survivor.id


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_does_not_re_add_an_excluded_item(
    db_session: Session, item_repo: ItemRepository
) -> None:
    """T8: a missing excluded item is left out; a missing arxiv item is restored."""
    model = add_item(
        item_repo,
        "huggingface_models",
        title="org/old-model",
        minute=0,
        status=ItemStatus.ENRICHED,
        embedded=True,
    )
    paper = add_item(
        item_repo, "arxiv", title="old-paper", minute=1, status=ItemStatus.ENRICHED, embedded=True
    )
    add_item(item_repo, "arxiv", title="new", minute=2)

    dedup, embedder = run_stage(item_repo)

    assert not dedup.contains(model.id)
    assert ("add", model.id) not in dedup.calls
    assert dedup.contains(paper.id)
    assert not any(text.startswith("org/old-model") for text in embedder.texts)


def test_reconciliation_removes_an_excluded_item_an_old_index_still_holds(
    db_session: Session, item_repo: ItemRepository
) -> None:
    """T9 (with doubles): the vector goes; the arxiv item already indexed stays."""
    model = add_item(
        item_repo,
        "huggingface_spaces",
        title="org/space",
        minute=0,
        status=ItemStatus.ENRICHED,
        embedded=True,
    )
    paper = add_item(
        item_repo, "arxiv", title="old-paper", minute=1, status=ItemStatus.ENRICHED, embedded=True
    )
    add_item(item_repo, "arxiv", title="new", minute=2)
    dedup = CosineDeduplicator()
    dedup.index = {model.id: SAME, paper.id: [0.0, 1.0, 0.0]}

    run_stage(item_repo, dedup=dedup)

    assert ("remove", model.id) in dedup.calls
    assert not dedup.contains(model.id)
    assert dedup.contains(paper.id)
    assert ("remove", paper.id) not in dedup.calls


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_skipped_items_are_logged_and_counted(item_repo: ItemRepository) -> None:
    """T10: one INFO line per excluded item, and the count on the completion line."""
    add_item(item_repo, "arxiv", title="paper", minute=0)
    model = add_item(item_repo, "huggingface_models", title="org/model", minute=1)
    space = add_item(item_repo, "huggingface_spaces", title="org/space", minute=2)

    with capture_logs() as logs:
        run_stage(item_repo)

    skipped = [e for e in logs if e["event"] == "semantic_dedup_skipped_excluded_source"]
    assert [e["source_id"] for e in skipped] == ["huggingface_models", "huggingface_spaces"]
    assert all(e["log_level"] == "info" for e in skipped)
    assert {e["item_id"] for e in skipped} == {model.id, space.id}
    completed = [e for e in logs if e["event"] == "embed_stage_completed"]
    assert completed[-1]["dedup_skipped_excluded_source"] == 2
    assert completed[-1]["enriched"] == 3
    assert completed[-1]["duplicates"] == 0
