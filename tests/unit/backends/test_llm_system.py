"""
Unit tests for the system message of SDS 5.9.1 (D-013).

``TransformersBackend`` against the fake runtime of ``test_llm.py``, and
``StubLLM``. No model is loaded and no GPU is needed.
"""

from __future__ import annotations

import pytest

from arip.backends.llm import transformers_backend as tb
from arip.backends.llm.stub_backend import StubLLM
from arip.backends.llm.transformers_backend import TransformersBackend
from arip.entities import GenerationParams
from tests.unit.backends.test_llm import MODEL, FakeRuntime


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    runtime = FakeRuntime()
    monkeypatch.setattr(tb, "_load_runtime", lambda: (runtime.torch, runtime.transformers))
    return runtime


def test_system_text_is_sent_as_system_message(fake: FakeRuntime) -> None:
    with TransformersBackend(MODEL) as llm:
        llm.generate("Write.", GenerationParams(), system="Be brief.")

    assert fake.tokenizer.template_calls[0]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Write."},
    ]
    assert fake.tokenizer.template_calls[0]["add_generation_prompt"] is True


def test_no_system_message_when_none(fake: FakeRuntime) -> None:
    with TransformersBackend(MODEL) as llm:
        llm.generate("Write.", GenerationParams())
        llm.generate("Again.", GenerationParams(), system=None)

    assert [call["messages"] for call in fake.tokenizer.template_calls] == [
        [{"role": "user", "content": "Write."}],
        [{"role": "user", "content": "Again."}],
    ]


def test_stub_records_system() -> None:
    first, second = GenerationParams(seed=1), GenerationParams(seed=2)
    with StubLLM() as stub:
        stub.generate("a", first, system="Be brief.")
        stub.generate("b", second)

    assert stub.systems == ["Be brief.", None]
    assert stub.calls == [("a", first), ("b", second)]
