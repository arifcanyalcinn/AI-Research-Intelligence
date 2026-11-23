"""
Unit tests for GenerateStage (SDS §5.10.2, §9.2.1, §3.3, D-015, D-017).

StubLLM with canned outputs, a real PromptManager over a temporary template
directory (the shipped thread_en.j2 and a TR copy of it), the real validator and
an in-memory SQLite database. No GPU, no model.

Every assertion about the database reads in a fresh session, so it sees only
what the stage committed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from arip.backends.llm.stub_backend import StubLLM
from arip.config import GenerationParams, ValidationConfig
from arip.db.database import build_session_factory, init_db, session_scope
from arip.db.models import GeneratedContent, Item
from arip.db.repositories.content import ContentRepository
from arip.db.repositories.items import ItemRepository
from arip.enums import ItemStatus, Language
from arip.exceptions import LLMError
from arip.generation.prompt_manager import PromptManager
from arip.generation.validator import ContentValidator
from arip.pipeline.stages.generate import GenerateStage, is_eligible, select_for_generation

REPO = Path(__file__).resolve().parents[3]
URL = "https://arxiv.org/abs/2411.01234"
ABSTRACT = "We teach small models to check each reasoning step before going on."
TEXTS = {
    "HOOK": "A new way to make small language models reason step by step.",
    "PROBLEM": "Small models often skip steps and reach wrong answers on maths problems.",
    "SOLUTION": "The authors train the model to write each step and check it before going on.",
    "IMPACT": "Teams with one GPU can get more reliable answers from a small open model.",
    "TAKEAWAY": "Checking each step matters more than making the model larger.",
    "SOURCE": f"Read the full paper on arXiv: {URL}",
}
VALID = "\n".join(f"[{role}] {text}" for role, text in TEXTS.items())
NO_URL = VALID.replace(f": {URL}", " today, it is free to read.")
NO_URL_ERROR = f"Citation: the SOURCE segment does not contain {URL}"
OOM = "CUDA out of memory during generate"
RUN_ID = 7


# ---------------------------------------------------------------------------
# Doubles and helpers
# ---------------------------------------------------------------------------


class CountingLLM(StubLLM):
    """StubLLM that counts loads and unloads and records the logging context of each call."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(model_name="Qwen/Qwen2.5-3B-Instruct", **kwargs)  # type: ignore[arg-type]
        self.entered = 0
        self.exited = 0
        self.contexts: list[dict] = []

    def __enter__(self) -> CountingLLM:
        self.entered += 1
        return super().__enter__()  # type: ignore[return-value]

    def __exit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        self.exited += 1
        super().__exit__(exc_type, exc_val, exc_tb)

    def generate(self, prompt, params, system=None):  # noqa: ANN001, ANN201
        self.contexts.append(dict(structlog.contextvars.get_contextvars()))
        return super().generate(prompt, params, system)


class FailingLoadLLM(CountingLLM):
    """A backend whose model load fails, as §5.9's out-of-memory case does."""

    def __enter__(self) -> CountingLLM:
        self.entered += 1
        raise LLMError("VRAM insufficient for Qwen/Qwen2.5-3B-Instruct. Try a smaller model.")


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    yield build_session_factory(engine)
    engine.dispose()


@pytest.fixture
def prompts(tmp_path: Path) -> PromptManager:
    """The shipped EN template and a TR copy of it, written as bytes (no newline translation)."""
    text = (REPO / "config" / "prompts" / "thread_en.j2").read_text(encoding="utf-8")
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "thread_en.j2").write_bytes(text.encode("utf-8"))
    (directory / "thread_tr.j2").write_bytes(
        text.replace("language: EN", "language: TR", 1).encode("utf-8")
    )
    return PromptManager(directory)


def add_item(
    session_factory: sessionmaker[Session],
    name: str,
    score: float = 0.5,
    **overrides: object,
) -> int:
    data: dict[str, object] = {
        "source_id": "arxiv",
        "source_type": "PAPER",
        "external_id": name,
        "content_hash": f"hash-{name}",
        "status": ItemStatus.ENRICHED.value,
        "title": f"Paper {name}",
        "primary_url": URL,
        "abstract": ABSTRACT,
        "language": "EN",
        "importance_score": score,
    }
    data.update(overrides)
    with session_scope(session_factory) as session:
        return ItemRepository(session).create(data).id


def make_stage(
    session_factory: sessionmaker[Session],
    prompts: PromptManager,
    llm: CountingLLM,
    cap: int = 50,
    languages: tuple[Language, ...] = (Language.EN,),
    max_retries: int = 2,
) -> tuple[GenerateStage, list[int]]:
    """The stage, and a list that grows by one each time the LLM factory is called."""
    factory_calls: list[int] = []

    def llm_factory() -> CountingLLM:
        factory_calls.append(1)
        return llm

    stage = GenerateStage(
        session_factory=session_factory,
        llm_factory=llm_factory,
        prompt_manager=prompts,
        validator=ContentValidator(ValidationConfig()),
        params=GenerationParams(seed=100),
        max_retries=max_retries,
        languages=languages,
        max_items_per_run=cap,
    )
    return stage, factory_calls


def item(session_factory: sessionmaker[Session], item_id: int) -> Item:
    with session_factory() as session:
        found = session.get(Item, item_id)
        assert found is not None
        return found


def status(session_factory: sessionmaker[Session], item_id: int) -> str:
    return item(session_factory, item_id).status


def rows(session_factory: sessionmaker[Session], item_id: int) -> list[GeneratedContent]:
    with session_factory() as session:
        return list(
            session.execute(
                select(GeneratedContent)
                .where(GeneratedContent.item_id == item_id)
                .order_by(GeneratedContent.attempt)
            )
            .scalars()
            .all()
        )


def completed(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry["event"] == "generation_stage_completed"]


# ---------------------------------------------------------------------------
# Selection (§9.2.1)
# ---------------------------------------------------------------------------


def test_selection_follows_d010_order(session_factory, prompts) -> None:  # noqa: ANN001
    low = add_item(session_factory, "low", score=0.4)
    high = add_item(session_factory, "high", score=0.9)
    middle = add_item(session_factory, "middle", score=0.6)
    llm = CountingLLM(responses=[VALID])
    stage, _ = make_stage(session_factory, prompts, llm, cap=2)

    stage.run(RUN_ID)

    assert status(session_factory, high) == ItemStatus.GENERATED.value
    assert status(session_factory, middle) == ItemStatus.GENERATED.value
    assert status(session_factory, low) == ItemStatus.ENRICHED.value
    assert [ctx["item_id"] for ctx in llm.contexts] == [high, middle]


def test_cap_limits_selected_items(session_factory, prompts) -> None:  # noqa: ANN001
    for name in ("a", "b", "c"):
        add_item(session_factory, name)
    llm = CountingLLM(responses=[VALID])
    stage, _ = make_stage(session_factory, prompts, llm, cap=2)

    stage.run(RUN_ID)

    assert len(llm.calls) == 2
    assert len({ctx["item_id"] for ctx in llm.contexts}) == 2


def test_cap_is_per_language(session_factory, prompts) -> None:  # noqa: ANN001
    en = [add_item(session_factory, f"en{i}", score=0.9 - i / 10) for i in range(2)]
    tr = [add_item(session_factory, f"tr{i}", score=0.9 - i / 10, language="TR") for i in range(2)]
    llm = CountingLLM(responses=[VALID])
    stage, _ = make_stage(
        session_factory, prompts, llm, cap=1, languages=(Language.EN, Language.TR)
    )

    stage.run(RUN_ID)

    assert [status(session_factory, i) for i in en] == ["GENERATED", "ENRICHED"]
    assert [status(session_factory, i) for i in tr] == ["GENERATED", "ENRICHED"]


def test_ineligible_items_do_not_count_against_cap(session_factory, prompts) -> None:  # noqa: ANN001
    ineligible = add_item(session_factory, "model", score=0.9, abstract=None)
    eligible = add_item(session_factory, "paper", score=0.5)
    llm = CountingLLM(responses=[VALID])
    stage, _ = make_stage(session_factory, prompts, llm, cap=1)

    stage.run(RUN_ID)

    assert status(session_factory, eligible) == ItemStatus.GENERATED.value
    assert status(session_factory, ineligible) == ItemStatus.ENRICHED.value


@pytest.mark.parametrize("abstract", [None, "", " \n\t "], ids=["none", "empty", "whitespace"])
def test_blank_abstract_is_ineligible(session_factory, prompts, abstract) -> None:  # noqa: ANN001
    blank = add_item(session_factory, "blank", score=0.9, abstract=abstract)
    eligible = add_item(session_factory, "paper", score=0.5)
    llm = CountingLLM(responses=[VALID])
    stage, _ = make_stage(session_factory, prompts, llm, cap=1)

    stage.run(RUN_ID)

    assert status(session_factory, blank) == ItemStatus.ENRICHED.value
    assert rows(session_factory, blank) == []
    assert status(session_factory, eligible) == ItemStatus.GENERATED.value


def test_ineligible_item_is_untouched(session_factory, prompts) -> None:  # noqa: ANN001
    ineligible = add_item(session_factory, "model", abstract=None)
    add_item(session_factory, "paper")
    before = item(session_factory, ineligible)
    stage, _ = make_stage(session_factory, prompts, CountingLLM(responses=[VALID]))

    stage.run(RUN_ID)

    after = item(session_factory, ineligible)
    assert after.status == ItemStatus.ENRICHED.value
    assert after.failed_at_stage is None and after.failure_reason is None
    assert after.updated_at == before.updated_at
    assert rows(session_factory, ineligible) == []


def test_items_beyond_cap_stay_enriched(session_factory, prompts) -> None:  # noqa: ANN001
    add_item(session_factory, "first", score=0.9)
    beyond = [add_item(session_factory, name, score=0.5) for name in ("second", "third")]
    stage, _ = make_stage(session_factory, prompts, CountingLLM(responses=[VALID]), cap=1)

    stage.run(RUN_ID)

    for item_id in beyond:
        found = item(session_factory, item_id)
        assert found.status == ItemStatus.ENRICHED.value
        assert found.failure_reason is None
        assert rows(session_factory, item_id) == []


def test_failed_items_count_against_cap(session_factory, prompts) -> None:  # noqa: ANN001
    failing = add_item(session_factory, "a", score=0.9)
    passing = add_item(session_factory, "b", score=0.8)
    left = add_item(session_factory, "c", score=0.7)
    llm = CountingLLM(responses=[NO_URL, NO_URL, NO_URL, VALID])
    stage, _ = make_stage(session_factory, prompts, llm, cap=2)

    stage.run(RUN_ID)

    assert status(session_factory, failing) == ItemStatus.FAILED.value
    assert status(session_factory, passing) == ItemStatus.GENERATED.value
    assert status(session_factory, left) == ItemStatus.ENRICHED.value


def test_completion_log_counts(session_factory, prompts) -> None:  # noqa: ANN001
    add_item(session_factory, "model", score=0.95, abstract=None)
    add_item(session_factory, "a", score=0.9)
    add_item(session_factory, "b", score=0.8)
    add_item(session_factory, "c", score=0.7)
    llm = CountingLLM(responses=[NO_URL, NO_URL, NO_URL, VALID])
    stage, _ = make_stage(session_factory, prompts, llm, cap=2)

    with capture_logs() as logs:
        stage.run(RUN_ID)

    lines = completed(logs)
    assert len(lines) == 1
    line = lines[0]
    assert line["log_level"] == "info"
    assert {
        key: line[key]
        for key in (
            "run_id",
            "stage",
            "language",
            "enriched",
            "ineligible",
            "selected",
            "beyond_cap",
            "generated",
            "failed",
            "not_processed",
            "stopped",
        )
    } == {
        "run_id": RUN_ID,
        "stage": "generation",
        "language": "EN",
        "enriched": 4,
        "ineligible": 1,
        "selected": 2,
        "beyond_cap": 1,
        "generated": 1,
        "failed": 1,
        "not_processed": 0,
        "stopped": False,
    }
    assert isinstance(line["duration_ms"], int)
    started = [entry for entry in logs if entry["event"] == "generation_stage_started"]
    assert started[0]["languages"] == ["EN"] and started[0]["max_items_per_run"] == 2
    failed = [entry for entry in logs if entry["event"] == "item_generation_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "warning"


def test_select_for_generation_counts() -> None:
    def fake(item_id: int, abstract: str | None) -> Item:
        return Item(id=item_id, abstract=abstract)

    items = [fake(1, None), fake(2, "x"), fake(3, "  "), fake(4, "y"), fake(5, "z")]

    selection = select_for_generation(items, 2)

    assert selection.selected == [2, 4]
    assert (selection.enriched, selection.ineligible, selection.beyond_cap) == (5, 2, 1)
    assert not is_eligible(fake(6, " \n"))


# ---------------------------------------------------------------------------
# Transitions (§3.3, §5.10.2)
# ---------------------------------------------------------------------------


def test_valid_output_generates_item(session_factory, prompts) -> None:  # noqa: ANN001
    item_id = add_item(session_factory, "a")
    stage, _ = make_stage(session_factory, prompts, CountingLLM(responses=[VALID]))

    stage.run(RUN_ID)

    found = item(session_factory, item_id)
    assert found.status == ItemStatus.GENERATED.value
    assert found.failure_reason is None and found.failed_at_stage is None
    stored = rows(session_factory, item_id)
    assert [(r.attempt, r.is_active, r.validation_passed) for r in stored] == [(1, True, True)]


def test_generated_at_equals_active_row(session_factory, prompts) -> None:  # noqa: ANN001
    item_id = add_item(session_factory, "a")
    stage, _ = make_stage(session_factory, prompts, CountingLLM(responses=[NO_URL, VALID]))

    stage.run(RUN_ID)

    active = [r for r in rows(session_factory, item_id) if r.is_active]
    assert len(active) == 1
    assert item(session_factory, item_id).generated_at == active[0].generated_at


def test_exhausted_validation_fails_item(session_factory, prompts) -> None:  # noqa: ANN001
    item_id = add_item(session_factory, "a")
    stage, _ = make_stage(session_factory, prompts, CountingLLM(responses=[NO_URL]))

    stage.run(RUN_ID)

    found = item(session_factory, item_id)
    assert found.status == ItemStatus.FAILED.value
    assert found.failed_at_stage == "GENERATION"
    assert found.failure_reason == f"Validation failed after 3 attempts: {NO_URL_ERROR}"
    assert [r.attempt for r in rows(session_factory, item_id)] == [1, 2, 3]


def test_template_error_fails_item(session_factory, prompts) -> None:  # noqa: ANN001
    item_id = add_item(session_factory, "a", primary_url=None)
    llm = CountingLLM(responses=[VALID])
    stage, _ = make_stage(session_factory, prompts, llm)

    stage.run(RUN_ID)

    found = item(session_factory, item_id)
    assert found.status == ItemStatus.FAILED.value
    assert found.failed_at_stage == "GENERATION"
    assert found.failure_reason == (
        "Template error: Missing required template variable(s) for thread_en: primary_url"
    )
    assert rows(session_factory, item_id) == []
    assert llm.calls == []


def test_llm_error_keeps_earlier_attempt_rows(session_factory, prompts) -> None:  # noqa: ANN001
    """D-015 requirement 1: FAILED is committed together with the attempt stored before the error."""
    first = add_item(session_factory, "a", score=0.9)
    second = add_item(session_factory, "b", score=0.5)
    llm = CountingLLM(responses=[NO_URL, VALID], fail_on_calls=(2,), error=LLMError(OOM))
    stage, _ = make_stage(session_factory, prompts, llm)

    with pytest.raises(LLMError, match="out of memory"):
        stage.run(RUN_ID)

    found = item(session_factory, first)
    assert found.status == ItemStatus.FAILED.value
    assert found.failed_at_stage == "GENERATION"
    assert found.failure_reason == f"LLM error: {OOM}"
    stored = rows(session_factory, first)
    assert [(r.attempt, r.is_active, r.validation_passed) for r in stored] == [(1, False, False)]
    assert status(session_factory, second) == ItemStatus.ENRICHED.value


def test_earlier_items_stay_committed_when_a_later_item_raises(
    session_factory,  # noqa: ANN001
    prompts,  # noqa: ANN001
) -> None:
    first = add_item(session_factory, "a", score=0.9)
    second = add_item(session_factory, "b", score=0.5)
    llm = CountingLLM(responses=[VALID], fail_on_calls=(2,), error=RuntimeError("boom"))
    stage, _ = make_stage(session_factory, prompts, llm)

    with pytest.raises(RuntimeError, match="boom"):
        stage.run(RUN_ID)

    assert status(session_factory, first) == ItemStatus.GENERATED.value
    assert [r.is_active for r in rows(session_factory, first)] == [True]
    assert status(session_factory, second) == ItemStatus.ENRICHED.value
    assert rows(session_factory, second) == []


def test_unexpected_error_leaves_item_enriched(session_factory, prompts) -> None:  # noqa: ANN001
    item_id = add_item(session_factory, "a")
    llm = CountingLLM(responses=[NO_URL, VALID], fail_on_calls=(2,), error=RuntimeError("boom"))
    stage, _ = make_stage(session_factory, prompts, llm)

    with pytest.raises(RuntimeError, match="boom"):
        stage.run(RUN_ID)

    found = item(session_factory, item_id)
    assert found.status == ItemStatus.ENRICHED.value
    assert found.failure_reason is None and found.failed_at_stage is None
    assert rows(session_factory, item_id) == []


def test_failed_regeneration_leaves_no_active_row(session_factory, prompts) -> None:  # noqa: ANN001
    """D-015 point 2, ruling Q8 (b): a FAILED item has no active row."""
    item_id = add_item(session_factory, "a")
    with session_scope(session_factory) as session:
        ContentRepository(session).create(
            {
                "item_id": item_id,
                "language": "EN",
                "attempt": 1,
                "is_active": True,
                "prompt_template_id": "thread_en",
                "full_prompt": "earlier pass",
                "raw_output": VALID,
                "validation_passed": True,
                "llm_backend_id": "stub",
                "model_name": "stub:earlier",
                "temperature": 0.7,
                "max_tokens": 1024,
            }
        )
    stage, _ = make_stage(session_factory, prompts, CountingLLM(responses=[NO_URL]))

    stage.run(RUN_ID)

    assert status(session_factory, item_id) == ItemStatus.FAILED.value
    stored = rows(session_factory, item_id)
    assert [r.attempt for r in stored] == [1, 2, 3, 4]
    assert [r.is_active for r in stored] == [False, False, False, False]


def test_item_log_lines_carry_item_context(session_factory, prompts) -> None:  # noqa: ANN001
    first = add_item(session_factory, "a", score=0.9)
    second = add_item(session_factory, "b", score=0.5, source_id="github_trending")
    llm = CountingLLM(responses=[VALID])
    stage, _ = make_stage(session_factory, prompts, llm)

    stage.run(RUN_ID)

    assert llm.contexts == [
        {"run_id": RUN_ID, "stage": "generation", "item_id": first, "source_id": "arxiv"},
        {
            "run_id": RUN_ID,
            "stage": "generation",
            "item_id": second,
            "source_id": "github_trending",
        },
    ]
    assert structlog.contextvars.get_contextvars() == {}


# ---------------------------------------------------------------------------
# Stop rule and model lifecycle (§5.10.2, AD-05)
# ---------------------------------------------------------------------------


def test_llm_error_stops_the_pass(session_factory, prompts) -> None:  # noqa: ANN001
    ids = [add_item(session_factory, name, score=0.9 - i / 10) for i, name in enumerate("abc")]
    llm = CountingLLM(responses=[VALID], fail_on_calls=(1,), error=LLMError(OOM))
    stage, _ = make_stage(session_factory, prompts, llm)

    with capture_logs() as logs, pytest.raises(LLMError, match="out of memory"):
        stage.run(RUN_ID)

    assert len(llm.calls) == 1
    assert [status(session_factory, i) for i in ids] == ["FAILED", "ENRICHED", "ENRICHED"]
    stopped = [entry for entry in logs if entry["event"] == "generation_pass_stopped"]
    assert len(stopped) == 1 and stopped[0]["log_level"] == "error"
    assert stopped[0]["item_id"] == ids[0]
    line = completed(logs)[0]
    assert (line["selected"], line["failed"], line["not_processed"], line["stopped"]) == (
        3,
        1,
        2,
        True,
    )


def test_load_failure_changes_no_item(session_factory, prompts) -> None:  # noqa: ANN001
    ids = [add_item(session_factory, name) for name in "ab"]
    llm = FailingLoadLLM(responses=[VALID])
    stage, _ = make_stage(session_factory, prompts, llm)

    with capture_logs() as logs, pytest.raises(LLMError, match="VRAM insufficient"):
        stage.run(RUN_ID)

    for item_id in ids:
        found = item(session_factory, item_id)
        assert found.status == ItemStatus.ENRICHED.value
        assert found.failure_reason is None
        assert rows(session_factory, item_id) == []
    assert llm.calls == []
    line = completed(logs)[0]
    assert (line["selected"], line["not_processed"], line["stopped"]) == (2, 2, True)


def test_llm_unloaded_after_pass(session_factory, prompts) -> None:  # noqa: ANN001
    add_item(session_factory, "a")
    llm = CountingLLM(responses=[VALID])
    stage, _ = make_stage(session_factory, prompts, llm)

    stage.run(RUN_ID)

    assert (llm.entered, llm.exited, llm.is_loaded) == (1, 1, False)


def test_llm_unloaded_after_stop(session_factory, prompts) -> None:  # noqa: ANN001
    add_item(session_factory, "a")
    llm = CountingLLM(responses=[VALID], fail_on_calls=(1,), error=LLMError(OOM))
    stage, _ = make_stage(session_factory, prompts, llm)

    with pytest.raises(LLMError):
        stage.run(RUN_ID)

    assert (llm.entered, llm.exited, llm.is_loaded) == (1, 1, False)


def test_no_selection_loads_no_model(session_factory, prompts) -> None:  # noqa: ANN001
    add_item(session_factory, "model", abstract=None)
    llm = CountingLLM(responses=[VALID])
    stage, factory_calls = make_stage(session_factory, prompts, llm)

    with capture_logs() as logs:
        stage.run(RUN_ID)

    assert factory_calls == [] and llm.entered == 0
    line = completed(logs)[0]
    assert (line["enriched"], line["ineligible"], line["selected"]) == (1, 1, 0)


def test_one_model_load_per_pass(session_factory, prompts) -> None:  # noqa: ANN001
    add_item(session_factory, "en")
    add_item(session_factory, "tr", language="TR")
    llm = CountingLLM(responses=[VALID])
    stage, factory_calls = make_stage(
        session_factory, prompts, llm, languages=(Language.EN, Language.TR)
    )

    stage.run(RUN_ID)

    assert factory_calls == [1]
    assert (llm.entered, llm.exited) == (1, 1)
    assert len(llm.calls) == 2
