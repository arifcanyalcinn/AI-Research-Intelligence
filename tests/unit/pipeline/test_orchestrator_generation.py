"""
Unit tests for the generation step of PipelineOrchestrator (SDS §5.10.2, §5.15.3, AD-06).

A run here goes collect → rank → embed → generate against an in-memory SQLite
database. There are no sources: the item is seeded RANKED, with an abstract, so
the embedding stage enters the embedder and passes the item to generation. The
embedder and deduplicator are doubles (as in test_orchestrator.py); the LLM is
StubLLM, recording into the same event log as the embedder, so the order of the
two models' context managers is observable.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from arip.backends.llm.stub_backend import StubLLM
from arip.config import (
    GenerationParams,
    RankingSettings,
    SignalWeights,
    ValidationConfig,
    load_settings,
)
from arip.container import _build_generation
from arip.db.database import build_session_factory, init_db, session_scope
from arip.db.models import Item, PipelineRun
from arip.db.repositories.items import ItemRepository
from arip.enums import ItemStatus, Language, PipelineRunStatus
from arip.exceptions import LLMError
from arip.generation.prompt_manager import PromptManager
from arip.generation.validator import ContentValidator
from arip.pipeline.orchestrator import PipelineOrchestrator
from arip.pipeline.stages.generate import GenerateStage
from arip.ranking.scorer import Scorer

REPO = Path(__file__).resolve().parents[3]
URL = "https://arxiv.org/abs/2411.01234"
ABSTRACT = "We teach small models to check each reasoning step before going on."
VALID = "\n".join(
    [
        "[HOOK] A new way to make small language models reason step by step.",
        "[PROBLEM] Small models often skip steps and reach wrong answers on maths problems.",
        "[SOLUTION] The authors train the model to write each step and check it before going on.",
        "[IMPACT] Teams with one GPU can get more reliable answers from a small open model.",
        "[TAKEAWAY] Checking each step matters more than making the model larger.",
        f"[SOURCE] Read the full paper on arXiv: {URL}",
    ]
)


class _Registry:
    def get_active_sources(self) -> list:
        return []


class _Embedder:
    """BaseEmbedder-shaped double writing its lifecycle into a shared event log."""

    backend_id = "_gen_fake"
    embedding_dim = 4
    model_name = "fake:model"

    def __init__(self, events: list[str], fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.entered = 0
        self.exited = 0

    def __enter__(self) -> _Embedder:
        self.entered += 1
        self.events.append("embed_enter")
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.exited += 1
        self.events.append("embed_exit")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedder exploded")
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class _Deduplicator:
    def __init__(self, embedding_dim: int) -> None:
        self.added: list[int] = []

    def load_index(self) -> None:
        pass

    def save_index(self) -> None:
        pass

    def nearest(self, vector: object, item_id: int) -> tuple[int | None, float]:
        return (None, 0.0)

    def is_duplicate(self, vector: object, item_id: int) -> tuple[bool, int | None]:
        return (False, None)

    def contains(self, item_id: int) -> bool:
        return item_id in self.added

    def add(self, item_id: int, vector: object) -> None:
        self.added.append(item_id)

    def remove(self, item_id: int) -> bool:
        return False

    @property
    def size(self) -> int:
        return len(self.added)


class _LLM(StubLLM):
    """StubLLM that logs its lifecycle and what the embedder had done when it was entered."""

    def __init__(self, events: list[str], embedder: _Embedder, **kwargs: object) -> None:
        super().__init__(model_name="Qwen/Qwen2.5-3B-Instruct", **kwargs)  # type: ignore[arg-type]
        self.events = events
        self.embedder = embedder
        self.embedder_at_enter: list[tuple[int, int]] = []

    def __enter__(self) -> _LLM:
        self.embedder_at_enter.append((self.embedder.entered, self.embedder.exited))
        self.events.append("llm_enter")
        return super().__enter__()  # type: ignore[return-value]

    def __exit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        self.events.append("llm_exit")
        super().__exit__(exc_type, exc_val, exc_tb)


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    yield build_session_factory(engine)
    engine.dispose()


def add_item(
    session_factory: sessionmaker[Session], name: str, status: ItemStatus, score: float = 0.6
) -> int:
    with session_scope(session_factory) as session:
        return (
            ItemRepository(session)
            .create(
                {
                    "source_id": "arxiv",
                    "source_type": "PAPER",
                    "external_id": name,
                    "content_hash": f"hash-{name}",
                    "status": status.value,
                    "title": f"Paper {name}",
                    "primary_url": URL,
                    "abstract": ABSTRACT,
                    "language": "EN",
                    "importance_score": score,
                }
            )
            .id
        )


def build(
    session_factory: sessionmaker[Session],
    embedder: _Embedder,
    llm: _LLM | None,
    factory_calls: list[int] | None = None,
) -> PipelineOrchestrator:
    """An orchestrator; ``llm=None`` builds it with generation disabled (no factory)."""
    generate_stage_factory = None
    if llm is not None:

        def llm_factory() -> _LLM:
            if factory_calls is not None:
                factory_calls.append(1)
            return llm

        generate_stage_factory = partial(
            GenerateStage,
            llm_factory=llm_factory,
            prompt_manager=PromptManager(REPO / "config" / "prompts"),
            validator=ContentValidator(ValidationConfig()),
            params=GenerationParams(seed=100),
            max_retries=2,
            languages=[Language.EN],
            max_items_per_run=50,
        )
    ranking = RankingSettings(
        min_score=0.35,
        recency_k=0.15,
        weights=SignalWeights(),
        topic_keywords=[],
        source_authority={"arxiv": 0.85},
    )
    return PipelineOrchestrator(
        registry=_Registry(),  # type: ignore[arg-type]
        session_factory=session_factory,
        scorer=Scorer(ranking),
        min_score=ranking.min_score,
        embedder_factory=lambda: embedder,  # type: ignore[arg-type,return-value]
        deduplicator_factory=_Deduplicator,  # type: ignore[arg-type]
        generate_stage_factory=generate_stage_factory,
    )


def status(session_factory: sessionmaker[Session], item_id: int) -> str:
    with session_factory() as session:
        found = session.get(Item, item_id)
        assert found is not None
        return found.status


def run_row(session_factory: sessionmaker[Session], run_id: int | None = None) -> PipelineRun:
    with session_factory() as session:
        runs = session.query(PipelineRun).order_by(PipelineRun.id).all()
        return runs[-1] if run_id is None else next(r for r in runs if r.id == run_id)


# ---------------------------------------------------------------------------
# Ordering (AD-06)
# ---------------------------------------------------------------------------


def test_generation_follows_embedding(session_factory) -> None:  # noqa: ANN001
    item_id = add_item(session_factory, "a", ItemStatus.RANKED)
    events: list[str] = []
    embedder = _Embedder(events)
    llm = _LLM(events, embedder, responses=[VALID])

    run_id = build(session_factory, embedder, llm).run_once()

    assert events == ["embed_enter", "embed_exit", "llm_enter", "llm_exit"]
    assert status(session_factory, item_id) == ItemStatus.GENERATED.value
    assert run_row(session_factory, run_id).status == PipelineRunStatus.COMPLETED.value


def test_llm_entered_after_embedder_exited(session_factory) -> None:  # noqa: ANN001
    add_item(session_factory, "a", ItemStatus.RANKED)
    events: list[str] = []
    embedder = _Embedder(events)
    llm = _LLM(events, embedder, responses=[VALID])

    build(session_factory, embedder, llm).run_once()

    assert llm.embedder_at_enter == [(1, 1)]


def test_failed_embedding_skips_generation(session_factory) -> None:  # noqa: ANN001
    add_item(session_factory, "ranked", ItemStatus.RANKED)
    waiting = add_item(session_factory, "enriched", ItemStatus.ENRICHED)
    events: list[str] = []
    embedder = _Embedder(events, fail=True)
    calls: list[int] = []
    llm = _LLM(events, embedder, responses=[VALID])

    with pytest.raises(RuntimeError, match="embedder exploded"):
        build(session_factory, embedder, llm, calls).run_once()

    assert calls == []
    assert "llm_enter" not in events
    assert status(session_factory, waiting) == ItemStatus.ENRICHED.value
    assert run_row(session_factory).status == PipelineRunStatus.FAILED.value


def test_disabled_generation_loads_nothing(
    session_factory,  # noqa: ANN001
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generation.enabled false: the container builds no stage, and the run ends after embedding."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARIP_GENERATION__ENABLED", raising=False)
    path = tmp_path / "settings.yaml"
    path.write_text(
        yaml.safe_dump(
            {"llm": {"backend": "stub", "model_name": "m"}, "generation": {"enabled": False}}
        ),
        encoding="utf-8",
    )
    wiring = _build_generation(load_settings(path), REPO / "config" / "prompts")
    waiting = add_item(session_factory, "enriched", ItemStatus.ENRICHED)
    orchestrator = build(session_factory, _Embedder([]), None)
    orchestrator._generate_stage_factory = wiring[1] if wiring else None

    with capture_logs() as logs:
        run_id = orchestrator.run_once()

    assert wiring is None
    assert status(session_factory, waiting) == ItemStatus.ENRICHED.value
    assert run_row(session_factory, run_id).status == PipelineRunStatus.COMPLETED.value
    disabled = [entry for entry in logs if entry["event"] == "generation_stage_disabled"]
    assert len(disabled) == 1 and disabled[0]["run_id"] == run_id
    assert [e for e in logs if e["event"].startswith("generation_stage_")] == disabled


# ---------------------------------------------------------------------------
# Run status after a stop (ruling Q6)
# ---------------------------------------------------------------------------


def test_stopped_pass_fails_the_run(session_factory) -> None:  # noqa: ANN001
    first = add_item(session_factory, "a", ItemStatus.ENRICHED, score=0.9)
    second = add_item(session_factory, "b", ItemStatus.ENRICHED, score=0.5)
    events: list[str] = []
    embedder = _Embedder(events)
    llm = _LLM(
        events,
        embedder,
        responses=[VALID],
        fail_on_calls=(1,),
        error=LLMError("CUDA out of memory during generate"),
    )

    with pytest.raises(LLMError, match="out of memory"):
        build(session_factory, embedder, llm).run_once()

    run = run_row(session_factory)
    assert run.status == PipelineRunStatus.FAILED.value
    assert run.error_summary == "CUDA out of memory during generate"
    assert status(session_factory, first) == ItemStatus.FAILED.value
    assert status(session_factory, second) == ItemStatus.ENRICHED.value
