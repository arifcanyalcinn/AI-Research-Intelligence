"""
Unit tests for SDS §5.7.1 against the real usearch index.

``SemanticDeduplicator.remove()`` on its own, and reconciliation clearing an
excluded source's vector from an index file written before §5.7.1, through a
save and a reload.

numpy and usearch arrive with the optional `embedding` extra, so the module
skips rather than fails on a `[dev]`-only install, as test_semantic.py does.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

np = pytest.importorskip(
    "numpy",
    reason="numpy arrives with the optional `embedding` extra; "
    'install with `pip install -e ".[embedding]"` to run these tests',
)
pytest.importorskip(
    "usearch",
    reason="usearch arrives with the optional `embedding` extra; "
    'install with `pip install -e ".[embedding]"` to run these tests',
)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from arip.config import DedupSettings  # noqa: E402
from arip.db.database import init_db  # noqa: E402
from arip.db.repositories.items import ItemRepository  # noqa: E402
from arip.dedup.semantic import SemanticDeduplicator  # noqa: E402
from arip.enums import ItemStatus  # noqa: E402
from arip.pipeline.stages.embed import EmbedStage  # noqa: E402
from arip.state_machine import StateMachine  # noqa: E402

DIM = 8


def unit_vector(axis: int) -> np.ndarray:
    vector = np.zeros(DIM, dtype=np.float32)
    vector[axis] = 1.0
    return vector


def settings(tmp_path: Path) -> DedupSettings:
    return DedupSettings(
        semantic_threshold=0.92, ann_index_path=str(tmp_path / "ann_index.usearch"), top_k=5
    )


@pytest.fixture
def dedup(tmp_path: Path) -> SemanticDeduplicator:
    deduplicator = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    deduplicator.load_index()
    return deduplicator


# ---------------------------------------------------------------------------
# remove()
# ---------------------------------------------------------------------------


def test_remove_takes_a_present_vector_out_of_the_index(dedup: SemanticDeduplicator) -> None:
    """T11: True, gone from contains(), size and search results."""
    dedup.add(1, unit_vector(0))
    dedup.add(2, unit_vector(1))

    assert dedup.remove(1) is True

    assert not dedup.contains(1)
    assert dedup.size == 1
    assert dedup.nearest(unit_vector(0), 99)[0] == 2


def test_remove_of_an_absent_key_returns_false(dedup: SemanticDeduplicator) -> None:
    """T12: not an error, and nothing else changes."""
    dedup.add(2, unit_vector(1))

    assert dedup.remove(1) is False

    assert dedup.size == 1
    assert dedup.contains(2)


def test_remove_before_load_index_raises(tmp_path: Path) -> None:
    """T13: like every other method, never a silent no-op on an unloaded index."""
    unloaded = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)

    with pytest.raises(RuntimeError, match="load_index"):
        unloaded.remove(1)


# ---------------------------------------------------------------------------
# Reconciliation against a real index file — T9
# ---------------------------------------------------------------------------


class AxisEmbedder:
    """Embeds every text to the same unit vector on axis 3."""

    backend_id = "fake"
    embedding_dim = DIM
    model_name = "fake-model"

    def __enter__(self) -> AxisEmbedder:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.stack([unit_vector(3) for _ in texts])


def test_reconciliation_clears_an_excluded_vector_from_an_existing_index_file(
    tmp_path: Path,
) -> None:
    """T9: an index written before §5.7.1 loses the model's vector, through save and reload."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    repo = ItemRepository(session)

    def item(source_id: str, source_type: str, name: str, status: ItemStatus, embedded: bool):
        return repo.create(
            {
                "source_id": source_id,
                "source_type": source_type,
                "external_id": name,
                "content_hash": f"hash-{name}",
                "status": status.value,
                "language": "EN",
                "title": name,
                "collected_at": datetime(2026, 11, 1, 9, 0, 0),
                "signal_breakdown": json.dumps({"recency": 0.5}),
                "embedding_computed_at": datetime(2026, 10, 1) if embedded else None,
            }
        )

    model = item("huggingface_models", "MODEL", "org/model", ItemStatus.ENRICHED, True)
    paper = item("arxiv", "PAPER", "old-paper", ItemStatus.ENRICHED, True)
    item("arxiv", "PAPER", "new-paper", ItemStatus.RANKED, False)

    old = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    old.load_index()
    old.add(model.id, unit_vector(0))
    old.add(paper.id, unit_vector(1))
    old.save_index()

    current = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    stage = EmbedStage(
        item_repo=repo,
        state_machine=StateMachine(repo),
        embedder=AxisEmbedder(),  # type: ignore[arg-type]
        deduplicator_factory=lambda embedding_dim: current,
    )
    returned = stage.run(1)
    session.commit()
    returned.save_index()

    reloaded = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    reloaded.load_index()
    assert not reloaded.contains(model.id)
    assert reloaded.contains(paper.id)
    assert reloaded.size == 2  # the old paper and the new one
    session.close()
    engine.dispose()
