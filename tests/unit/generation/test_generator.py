"""
Unit tests for ContentGenerator (SDS 5.10, 5.10.1, D-015).

StubLLM with canned outputs, the shipped thread_en.j2, the real validator and
an in-memory SQLite database. No GPU, no model.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from arip.backends.llm.stub_backend import StubLLM
from arip.config import GenerationParams, ValidationConfig
from arip.db.database import init_db
from arip.db.models import GeneratedContent, Item
from arip.db.repositories.content import ContentRepository
from arip.db.repositories.items import ItemRepository
from arip.entities import GenerationResult
from arip.enums import ItemStatus, Language
from arip.exceptions import LLMError
from arip.generation import generator as generator_module
from arip.generation.context import build_context
from arip.generation.generator import ContentGenerator, attempt_seed
from arip.generation.prompt_manager import PromptManager, stored_prompt
from arip.generation.validator import ContentValidator

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
UNPARSED = "Here is a thread without any markers."
NO_URL_ERROR = f"Citation: the SOURCE segment does not contain {URL}"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def manager() -> PromptManager:
    return PromptManager(REPO / "config" / "prompts")


def make_item(session: Session, name: str = "1", **overrides: object) -> Item:
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
    }
    data.update(overrides)
    return ItemRepository(session).create(data)


def make_generator(
    session: Session,
    manager: PromptManager,
    responses: list[str | GenerationResult],
    max_retries: int = 2,
    seed: int | None = 100,
    stop_reason: str = "eos",
    fail_on_calls: tuple[int, ...] = (),
) -> tuple[ContentGenerator, StubLLM]:
    stub = StubLLM(
        model_name="Qwen/Qwen2.5-3B-Instruct",
        responses=responses,
        stop_reason=stop_reason,
        fail_on_calls=fail_on_calls,
        error=LLMError("CUDA out of memory during generate"),
    )
    stub.__enter__()
    generator = ContentGenerator(
        llm=stub,
        prompt_manager=manager,
        validator=ContentValidator(ValidationConfig()),
        content_repo=ContentRepository(session),
        params=GenerationParams(seed=seed),
        max_retries=max_retries,
    )
    return generator, stub


def rows(session: Session, item: Item) -> list[GeneratedContent]:
    return list(
        session.execute(
            select(GeneratedContent)
            .where(GeneratedContent.item_id == item.id)
            .order_by(GeneratedContent.attempt)
        )
        .scalars()
        .all()
    )


def sent_seeds(stub: StubLLM) -> list[int | None]:
    return [params.seed for _, params in stub.calls]


# ---------------------------------------------------------------------------
# Success path and stored row
# ---------------------------------------------------------------------------


def test_valid_output_gives_one_active_row(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [VALID])

    content = generator.generate(item)

    assert content is not None and content.id is not None
    assert content.is_active and content.validation_passed
    assert len(stub.calls) == 1
    assert generator.last_failure_reason is None
    assert rows(session, item) == [content]


def test_row_records_generation_metadata(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, _ = make_generator(session, manager, [VALID])

    content = generator.generate(item)

    assert (content.item_id, content.language, content.attempt) == (item.id, "EN", 1)
    assert content.prompt_template_id == "thread_en"
    assert (content.llm_backend_id, content.model_name) == ("stub", "stub:Qwen/Qwen2.5-3B-Instruct")
    assert (content.temperature, content.max_tokens, content.seed) == (0.7, 1024, 100)
    assert content.generation_latency_ms == 0
    assert content.generated_at is not None
    assert content.raw_output == VALID
    assert json.loads(content.parsed_segments) == [
        {"role": role, "text": text} for role, text in TEXTS.items()
    ]
    assert content.full_text == "\n\n".join(TEXTS.values())
    assert content.validation_errors == "[]"


def test_full_prompt_stored_in_d013_layout(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [VALID])

    content = generator.generate(item)

    rendered = manager.render("thread", Language.EN, build_context(item))
    system = manager.get_template("thread", Language.EN).system_text
    assert stub.calls[0][0] == rendered
    assert content.full_prompt == stored_prompt(system, rendered)


def test_system_text_sent_with_every_attempt(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [NO_URL, NO_URL, VALID])

    generator.generate(item)

    system = manager.get_template("thread", Language.EN).system_text
    assert stub.systems == [system, system, system]


def test_truncation_warning_stored(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, _ = make_generator(session, manager, [VALID], stop_reason="max_tokens")

    content = generator.generate(item)

    assert content.validation_passed
    assert json.loads(content.validation_errors) == [
        "WARNING: Truncation: generation stopped at max_new_tokens; the text may be cut off"
    ]


# ---------------------------------------------------------------------------
# Retries, attempts and active flags
# ---------------------------------------------------------------------------


def test_fail_fail_pass_active_flags(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, _ = make_generator(session, manager, [NO_URL, NO_URL, VALID])

    content = generator.generate(item)

    stored = rows(session, item)
    assert [r.attempt for r in stored] == [1, 2, 3]
    assert [r.is_active for r in stored] == [False, False, True]
    assert [r.validation_passed for r in stored] == [False, False, True]
    assert stored[-1] is content
    assert json.loads(stored[0].validation_errors) == [NO_URL_ERROR]


def test_max_retries_two_gives_three_attempts(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [NO_URL])

    assert generator.generate(item) is None
    assert len(stub.calls) == 3
    assert [r.is_active for r in rows(session, item)] == [False, False, False]


def test_max_retries_zero_gives_one_attempt(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [NO_URL, VALID], max_retries=0)

    assert generator.generate(item) is None
    assert len(stub.calls) == 1
    assert len(rows(session, item)) == 1


def test_negative_max_retries_rejected(session: Session, manager: PromptManager) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        make_generator(session, manager, [VALID], max_retries=-1)


def test_parse_failure_is_retried(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [UNPARSED, VALID])

    content = generator.generate(item)

    first, second = rows(session, item)
    assert len(stub.calls) == 2
    assert (first.parsed_segments, first.full_text, first.raw_output) == (None, None, UNPARSED)
    assert json.loads(first.validation_errors) == [
        "Parse: the output could not be parsed into the six marked segments"
    ]
    assert second is content and second.is_active


def test_second_pass_continues_attempt_numbers(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    first_pass, _ = make_generator(session, manager, [NO_URL])
    assert first_pass.generate(item) is None
    second_pass, stub = make_generator(session, manager, [VALID])

    content = second_pass.generate(item)

    assert content.attempt == 4
    assert sent_seeds(stub) == [103]
    assert [r.attempt for r in rows(session, item)] == [1, 2, 3, 4]


def test_second_pass_leaves_one_active_row(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    first, _ = make_generator(session, manager, [VALID])
    old = first.generate(item)
    second, _ = make_generator(session, manager, [VALID])

    new = second.generate(item)

    assert [r.is_active for r in rows(session, item)] == [False, True]
    assert old.is_active is False
    assert ContentRepository(session).get_active_for_item(item.id) is new


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


def test_seeds_are_consecutive_and_stored(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [NO_URL, NO_URL, VALID], seed=100)

    generator.generate(item)

    assert sent_seeds(stub) == [100, 101, 102]
    assert [r.seed for r in rows(session, item)] == [100, 101, 102]


def test_null_config_seed_draws_a_base(
    session: Session, manager: PromptManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(generator_module.secrets, "randbelow", lambda n: 7 if n == 2**32 else -1)
    item = make_item(session)
    generator, stub = make_generator(session, manager, [NO_URL, NO_URL, VALID], seed=None)

    generator.generate(item)

    assert sent_seeds(stub) == [7, 8, 9]
    assert [r.seed for r in rows(session, item)] == [7, 8, 9]


def test_base_seed_ignores_global_random_state(session: Session, manager: PromptManager) -> None:
    first_item, second_item = make_item(session, "1"), make_item(session, "2")
    generator, stub = make_generator(session, manager, [VALID], seed=None)

    random.seed(1)
    generator.generate(first_item)
    random.seed(1)
    generator.generate(second_item)

    first, second = sent_seeds(stub)
    assert first != second
    assert all(0 <= s < 2**32 for s in (first, second))


def test_seed_wraps_modulo_2_32(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [NO_URL, NO_URL, VALID], seed=2**32 - 1)

    generator.generate(item)

    assert sent_seeds(stub) == [2**32 - 1, 0, 1]
    assert attempt_seed(2**32 - 1, 1) == 2**32 - 1


def test_only_the_seed_changes_between_attempts(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [NO_URL, NO_URL, VALID])

    generator.generate(item)

    prompts = {prompt for prompt, _ in stub.calls}
    others = [{**vars(p), "seed": None} for _, p in stub.calls]
    assert len(prompts) == 1
    assert others[0] == others[1] == others[2]
    assert others[0] == {
        "temperature": 0.7,
        "max_new_tokens": 1024,
        "top_p": 0.95,
        "repetition_penalty": 1.1,
        "seed": None,
        "stop_sequences": [],
    }


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_llm_error_is_not_retried(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, stub = make_generator(session, manager, [NO_URL, VALID], fail_on_calls=(2,))

    with pytest.raises(LLMError, match="out of memory"):
        generator.generate(item)

    assert len(stub.calls) == 2
    assert [r.attempt for r in rows(session, item)] == [1]
    assert generator.last_failure_reason == "LLM error: CUDA out of memory during generate"


def test_missing_required_variable_fails_without_llm_call(
    session: Session, manager: PromptManager
) -> None:
    item = make_item(session, primary_url=None)
    generator, stub = make_generator(session, manager, [VALID])

    assert generator.generate(item) is None

    assert stub.calls == []
    assert rows(session, item) == []
    assert generator.last_failure_reason == (
        "Template error: Missing required template variable(s) for thread_en: primary_url"
    )


def test_failure_reasons(session: Session, manager: PromptManager) -> None:
    item = make_item(session)
    generator, _ = make_generator(session, manager, [UNPARSED, UNPARSED, NO_URL])

    assert generator.generate(item) is None

    assert generator.last_failure_reason == f"Validation failed after 3 attempts: {NO_URL_ERROR}"


def test_failure_reason_reset_on_success(session: Session, manager: PromptManager) -> None:
    failing, passing = make_item(session, "1"), make_item(session, "2")
    generator, _ = make_generator(session, manager, [NO_URL, NO_URL, NO_URL, VALID])

    assert generator.generate(failing) is None
    assert generator.last_failure_reason is not None
    assert generator.generate(passing) is not None
    assert generator.last_failure_reason is None


def test_generator_does_not_commit(
    session: Session, manager: PromptManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = make_item(session)
    generator, _ = make_generator(session, manager, [NO_URL, VALID])

    def no_commit() -> None:
        raise AssertionError("the generator must not commit")

    monkeypatch.setattr(session, "commit", no_commit)

    assert generator.generate(item) is not None
