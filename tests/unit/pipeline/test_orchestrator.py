"""
Unit tests for PipelineOrchestrator.

Covers the Batch 7 scope defined in SDS §8.2:

  - the §4.6 concurrency guard: a run left RUNNING is reconciled to FAILED
    before a new run opens
  - per-run SourceHealth logging (§8, Phase 2 deliverable)
  - the run lifecycle: RUNNING → COMPLETED, or → FAILED with error_summary
  - collection is driven and its results are committed

and the Batch 8 addition (SDS §8.2):

  - ranking runs after collection in the same run, in its own session scope

Tested against an in-memory SQLite database. Sources are stubs, so no HTTP
occurs and respx is not needed; the registry is a test double for the reason
documented in test_collect.py.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from arip.config import RankingSettings, SignalWeights, SourceConfig
from arip.db.database import build_session_factory, init_db, session_scope
from arip.db.models import Item, PipelineRun
from arip.db.repositories.pipeline_runs import PipelineRunRepository
from arip.entities import NormalizedItem, RawSourcePayload, SourceHealth
from arip.enums import ItemStatus, PipelineRunStatus, SourceType
from arip.interfaces import BaseSource
from arip.pipeline.orchestrator import PipelineOrchestrator
from arip.ranking.scorer import Scorer
from arip.sources._http import compute_content_hash

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubSource(BaseSource):
    """Source returning canned payloads with a controllable health result."""

    source_id = "_orch_stub"
    source_type = SourceType.PAPER

    def __init__(
        self,
        payloads: list[RawSourcePayload] | None = None,
        *,
        source_id: str | None = None,
        health: SourceHealth | None = None,
        health_error: Exception | None = None,
        fetch_error: Exception | None = None,
    ) -> None:
        super().__init__(None)
        self._payloads = payloads or []
        self._health = health
        self._health_error = health_error
        self._fetch_error = fetch_error
        self.health_calls = 0
        if source_id is not None:
            self.source_id = source_id

    def fetch(self) -> list[RawSourcePayload]:
        if self._fetch_error is not None:
            raise self._fetch_error
        return list(self._payloads)

    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:
        title = str(payload.raw_data.get("title", ""))
        return NormalizedItem(
            source_id=payload.source_id,
            source_type=payload.source_type.value,
            external_id=payload.external_id,
            content_hash=compute_content_hash(
                source_id=payload.source_id,
                external_id=payload.external_id,
                title=title,
            ),
            language="EN",
            title=title,
            primary_url="https://example.org/x",
            raw_payload=json.dumps(payload.raw_data),
        )

    def health_check(self) -> SourceHealth:
        self.health_calls += 1
        if self._health_error is not None:
            raise self._health_error
        if self._health is not None:
            return self._health
        return SourceHealth(source_id=self.source_id, is_healthy=True)

    @classmethod
    def get_config_schema(cls) -> type:
        return SourceConfig


class _Registry:
    """Minimal stand-in for SourceRegistry.get_active_sources()."""

    def __init__(self, *sources: BaseSource) -> None:
        self._sources = list(sources)

    def get_active_sources(self) -> list[BaseSource]:
        return list(self._sources)


class _ExplodingRegistry:
    """Registry whose source list access fails, to force a stage-level error."""

    def __init__(self) -> None:
        self.calls = 0

    def get_active_sources(self) -> list[BaseSource]:
        self.calls += 1
        # Succeed for the health pass, fail when collection asks.
        if self.calls > 1:
            raise RuntimeError("registry exploded during collection")
        return []


def make_payload(external_id: str = "p1", title: str = "A Paper") -> RawSourcePayload:
    return RawSourcePayload(
        source_id="_orch_stub",
        source_type=SourceType.PAPER,
        external_id=external_id,
        raw_data={"title": title},
        fetched_at=datetime(2026, 8, 24, 12, 0, 0),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def session_factory() -> sessionmaker:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    yield build_session_factory(engine)
    engine.dispose()


def ranking(**overrides: object) -> RankingSettings:
    """RankingSettings for orchestrator wiring tests."""
    base: dict[str, object] = {
        "min_score": 0.35,
        "recency_k": 0.15,
        "weights": SignalWeights(),
        "topic_keywords": [],
        "source_authority": {"arxiv": 0.85, "github_trending": 0.65},
    }
    base.update(overrides)
    return RankingSettings(**base)  # type: ignore[arg-type]


def build_orchestrator(
    registry: object,
    session_factory: sessionmaker,
    config: RankingSettings | None = None,
) -> PipelineOrchestrator:
    """Construct an orchestrator over any registry, real or double."""
    settings = config or ranking()
    return PipelineOrchestrator(
        registry=registry,  # type: ignore[arg-type]
        session_factory=session_factory,
        scorer=Scorer(settings),
        min_score=settings.min_score,
    )


@pytest.fixture
def make_orchestrator(session_factory: sessionmaker):
    def _factory(
        *sources: BaseSource, config: RankingSettings | None = None
    ) -> PipelineOrchestrator:
        return build_orchestrator(_Registry(*sources), session_factory, config)

    return _factory


def all_runs(session_factory: sessionmaker) -> list[PipelineRun]:
    with session_scope(session_factory) as session:
        return list(session.query(PipelineRun).order_by(PipelineRun.id).all())


def all_items(session_factory: sessionmaker) -> list[Item]:
    with session_scope(session_factory) as session:
        return list(session.query(Item).order_by(Item.id).all())


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


def test_run_once_returns_the_run_id(make_orchestrator) -> None:
    """The caller gets the id of the run it just executed."""
    run_id = make_orchestrator(_StubSource()).run_once()

    assert isinstance(run_id, int)
    assert run_id > 0


def test_successful_run_is_completed(make_orchestrator, session_factory) -> None:
    """A clean run ends COMPLETED with a completion time (§4.6)."""
    make_orchestrator(_StubSource([make_payload()])).run_once()

    run = all_runs(session_factory)[0]
    assert run.status == PipelineRunStatus.COMPLETED.value
    assert run.completed_at is not None


def test_run_row_exists_even_with_no_sources(make_orchestrator, session_factory) -> None:
    """An empty registry still produces a complete run record."""
    make_orchestrator().run_once()

    runs = all_runs(session_factory)
    assert len(runs) == 1
    assert runs[0].status == PipelineRunStatus.COMPLETED.value


def test_each_call_opens_a_new_run(make_orchestrator, session_factory) -> None:
    """Two invocations produce two distinct run rows."""
    orchestrator = make_orchestrator(_StubSource())
    first = orchestrator.run_once()
    second = orchestrator.run_once()

    assert first != second
    assert len(all_runs(session_factory)) == 2


def test_stage_metrics_stays_null(make_orchestrator, session_factory) -> None:
    """stage_metrics is a Phase 7 deliverable and is not written in Batch 7."""
    make_orchestrator(_StubSource([make_payload()])).run_once()

    assert all_runs(session_factory)[0].stage_metrics is None


# ---------------------------------------------------------------------------
# Collection is driven and committed
# ---------------------------------------------------------------------------


def test_run_once_collects_items(make_orchestrator, session_factory) -> None:
    """Payloads reach the database through the collection stage."""
    make_orchestrator(_StubSource([make_payload("a"), make_payload("b")])).run_once()

    assert len(all_items(session_factory)) == 2


def test_collected_items_are_committed(make_orchestrator, session_factory) -> None:
    """Items are durable after run_once() returns, not left in an open session."""
    make_orchestrator(_StubSource([make_payload()])).run_once()

    items = all_items(session_factory)
    assert len(items) == 1
    assert items[0].id is not None


def test_second_run_skips_already_collected_items(
    make_orchestrator, session_factory
) -> None:
    """Running twice does not duplicate items — the dedup check spans runs."""
    orchestrator = make_orchestrator(_StubSource([make_payload()]))
    orchestrator.run_once()
    orchestrator.run_once()

    assert len(all_items(session_factory)) == 1


# ---------------------------------------------------------------------------
# Concurrency guard — SDS §4.6
# ---------------------------------------------------------------------------


def test_crashed_run_is_reconciled_before_new_run(
    make_orchestrator, session_factory
) -> None:
    """A row left RUNNING from a dead process becomes FAILED at startup."""
    with session_scope(session_factory) as session:
        crashed = PipelineRunRepository(session).create()
        crashed_id = crashed.id

    make_orchestrator(_StubSource()).run_once()

    runs = {r.id: r for r in all_runs(session_factory)}
    assert runs[crashed_id].status == PipelineRunStatus.FAILED.value


def test_reconciled_run_keeps_null_completed_at(
    make_orchestrator, session_factory
) -> None:
    """The time of the crash is unknown, so none is invented."""
    with session_scope(session_factory) as session:
        crashed_id = PipelineRunRepository(session).create().id

    make_orchestrator(_StubSource()).run_once()

    runs = {r.id: r for r in all_runs(session_factory)}
    assert runs[crashed_id].completed_at is None


def test_new_run_proceeds_after_reconciliation(
    make_orchestrator, session_factory
) -> None:
    """Reconciliation does not block the new run — it clears the way for it."""
    with session_scope(session_factory) as session:
        PipelineRunRepository(session).create()

    new_run_id = make_orchestrator(_StubSource([make_payload()])).run_once()

    runs = {r.id: r for r in all_runs(session_factory)}
    assert runs[new_run_id].status == PipelineRunStatus.COMPLETED.value
    assert len(all_items(session_factory)) == 1


def test_only_one_run_is_running_at_a_time(make_orchestrator, session_factory) -> None:
    """After a completed run, nothing is left in RUNNING."""
    make_orchestrator(_StubSource()).run_once()

    statuses = [r.status for r in all_runs(session_factory)]
    assert PipelineRunStatus.RUNNING.value not in statuses


# ---------------------------------------------------------------------------
# Source health logging — SDS §8, Phase 2 deliverable
# ---------------------------------------------------------------------------


def test_health_check_called_once_per_active_source(make_orchestrator) -> None:
    """Every active source reports health exactly once per run."""
    a = _StubSource(source_id="src_a")
    b = _StubSource(source_id="src_b")

    make_orchestrator(a, b).run_once()

    assert a.health_calls == 1
    assert b.health_calls == 1


def test_unhealthy_source_does_not_stop_the_run(
    make_orchestrator, session_factory
) -> None:
    """An unhealthy report is logged; collection still proceeds."""
    source = _StubSource(
        [make_payload()],
        health=SourceHealth(source_id="_orch_stub", is_healthy=False, last_error="503"),
    )

    make_orchestrator(source).run_once()

    assert len(all_items(session_factory)) == 1
    assert all_runs(session_factory)[0].status == PipelineRunStatus.COMPLETED.value


def test_health_check_exception_does_not_stop_the_run(
    make_orchestrator, session_factory
) -> None:
    """A health_check() that raises is contained — collection is unaffected."""
    source = _StubSource([make_payload()], health_error=RuntimeError("probe blew up"))

    make_orchestrator(source).run_once()

    assert len(all_items(session_factory)) == 1
    assert all_runs(session_factory)[0].status == PipelineRunStatus.COMPLETED.value


def test_health_check_failure_of_one_source_does_not_skip_another(
    make_orchestrator,
) -> None:
    """One bad probe does not prevent the next source reporting."""
    broken = _StubSource(source_id="src_broken", health_error=RuntimeError("boom"))
    healthy = _StubSource(source_id="src_ok")

    make_orchestrator(broken, healthy).run_once()

    assert healthy.health_calls == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_source_fetch_failure_still_completes_the_run(
    make_orchestrator, session_factory
) -> None:
    """A source-level failure is contained by CollectStage, so the run completes."""
    make_orchestrator(_StubSource(fetch_error=RuntimeError("network down"))).run_once()

    assert all_runs(session_factory)[0].status == PipelineRunStatus.COMPLETED.value


def test_stage_failure_marks_run_failed(session_factory) -> None:
    """An error escaping the stage records the run as FAILED."""
    orchestrator = build_orchestrator(_ExplodingRegistry(), session_factory)

    with pytest.raises(RuntimeError):
        orchestrator.run_once()

    assert all_runs(session_factory)[0].status == PipelineRunStatus.FAILED.value


def test_stage_failure_records_error_summary(session_factory) -> None:
    """The reason survives in pipeline_runs.error_summary (§4.6).

    This is why the run row is written in its own session scope: sharing the
    stage's scope would roll the FAILED record back along with the stage.
    """
    orchestrator = build_orchestrator(_ExplodingRegistry(), session_factory)

    with pytest.raises(RuntimeError):
        orchestrator.run_once()

    assert "exploded" in all_runs(session_factory)[0].error_summary


def test_stage_failure_re_raises_to_the_caller(session_factory) -> None:
    """run_once() does not swallow the error — the CLI needs a non-zero exit."""
    orchestrator = build_orchestrator(_ExplodingRegistry(), session_factory)

    with pytest.raises(RuntimeError, match="registry exploded"):
        orchestrator.run_once()


def test_failed_run_is_reconcilable_on_the_next_start(session_factory) -> None:
    """A FAILED run is closed, so the next startup does not re-reconcile it."""
    failing = build_orchestrator(_ExplodingRegistry(), session_factory)
    with pytest.raises(RuntimeError):
        failing.run_once()

    build_orchestrator(_Registry(_StubSource()), session_factory).run_once()

    statuses = [r.status for r in all_runs(session_factory)]
    assert statuses.count(PipelineRunStatus.FAILED.value) == 1
    assert statuses.count(PipelineRunStatus.COMPLETED.value) == 1


# ---------------------------------------------------------------------------
# Ranking is driven — SDS §8.2, Batch 8
# ---------------------------------------------------------------------------


def test_collected_items_are_ranked_in_the_same_run(
    make_orchestrator, session_factory
) -> None:
    """Collection and ranking both run, so nothing is left in COLLECTED."""
    make_orchestrator(
        _StubSource([make_payload("a"), make_payload("b")]),
        config=ranking(min_score=0.0),
    ).run_once()

    statuses = {i.status for i in all_items(session_factory)}
    assert statuses == {ItemStatus.RANKED.value}


def test_low_scoring_items_are_filtered_in_the_same_run(
    make_orchestrator, session_factory
) -> None:
    """The stub source has no configured authority, so it scores below 0.35."""
    make_orchestrator(_StubSource([make_payload()])).run_once()

    assert all_items(session_factory)[0].status == ItemStatus.FILTERED.value


def test_nothing_remains_collected_after_a_run(
    make_orchestrator, session_factory
) -> None:
    """Every collected item leaves COLLECTED within the run that collected it."""
    make_orchestrator(_StubSource([make_payload("x"), make_payload("y")])).run_once()

    remaining = [
        i for i in all_items(session_factory) if i.status == ItemStatus.COLLECTED.value
    ]
    assert remaining == []


def test_ranked_items_carry_a_score(make_orchestrator, session_factory) -> None:
    """importance_score is persisted through the orchestrator path."""
    make_orchestrator(
        _StubSource([make_payload()]), config=ranking(min_score=0.0)
    ).run_once()

    item = all_items(session_factory)[0]
    assert item.importance_score is not None
    assert item.signal_breakdown is not None


def test_run_completes_when_there_is_nothing_to_rank(
    make_orchestrator, session_factory
) -> None:
    """An empty collection leaves ranking with no work and the run still closes."""
    make_orchestrator().run_once()

    assert all_runs(session_factory)[0].status == PipelineRunStatus.COMPLETED.value


def test_second_run_does_not_rerank_items(
    make_orchestrator, session_factory
) -> None:
    """Items ranked by the first run are not revisited by the second."""
    orchestrator = make_orchestrator(
        _StubSource([make_payload()]), config=ranking(min_score=0.0)
    )
    orchestrator.run_once()
    first = all_items(session_factory)[0].ranked_at

    orchestrator.run_once()

    assert all_items(session_factory)[0].ranked_at == first
