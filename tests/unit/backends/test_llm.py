"""
Unit tests for the LLM backends and their registry (SDS §5.9, D-008, D-011).

No model is loaded and no GPU is needed. ``StubLLM`` loads nothing by design,
and ``TransformersBackend`` runs against a fake torch/transformers runtime put
in place of ``transformers_backend._load_runtime`` — the module's only import
point for either library. The tests therefore behave the same with CPU torch,
CUDA torch, or no torch at all, and never download anything.

The fake tokenizer works on characters: one token per character, so token
counts are string lengths and decoded text is easy to predict.
"""

from __future__ import annotations

import subprocess
import sys
import weakref
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from structlog.testing import capture_logs

from arip.backends.llm import transformers_backend as tb
from arip.backends.llm.registry import LLMRegistry
from arip.backends.llm.stub_backend import STUB_MODEL_NAME_PREFIX, StubLLM
from arip.backends.llm.transformers_backend import TransformersBackend
from arip.config import LLMSettings
from arip.entities import GenerationParams, GenerationResult
from arip.exceptions import ConfigError, LLMError

REPO = Path(__file__).resolve().parents[3]
MIB = 1024 * 1024
MODEL = "Qwen/Qwen2.5-3B-Instruct"
SDS_OOM_MESSAGE = f"VRAM insufficient for {MODEL}. Try a smaller or more quantized model."

# ---------------------------------------------------------------------------
# Fake runtime
# ---------------------------------------------------------------------------

PAD, EOS, IM_END = 0, 1, 2
OFFSET = 10  # character tokens start here, clear of the special ids


def char_ids(text: str) -> list[int]:
    return [ord(c) + OFFSET for c in text]


class FakeRow:
    def __init__(self, values: list[int]) -> None:
        self._values = values

    def tolist(self) -> list[int]:
        return list(self._values)


class FakeTensor:
    """A one-row 2-D tensor: ``shape``, ``[0, a:]`` and ``tolist()``."""

    def __init__(self, row: list[int]) -> None:
        self.row = list(row)

    @property
    def shape(self) -> tuple[int, int]:
        return (1, len(self.row))

    def __getitem__(self, index: tuple[int, slice]) -> FakeRow:
        row, columns = index
        assert row == 0
        return FakeRow(self.row[columns])


class FakeEncoding(dict):
    device: str | None = None

    def to(self, device: str) -> FakeEncoding:
        self.device = device
        return self


class FakeConfig(SimpleNamespace):
    """Stands in for BitsAndBytesConfig and GenerationConfig: keeps its kwargs."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.kwargs = kwargs


class FakeTokenizer:
    eos_token_id = IM_END
    pad_token_id = PAD

    def __init__(self) -> None:
        self.template_calls: list[dict] = []
        self.encode_calls: list[dict] = []

    def apply_chat_template(self, messages, add_generation_prompt=False, tokenize=True):  # noqa: ANN001
        self.template_calls.append(
            {
                "messages": messages,
                "add_generation_prompt": add_generation_prompt,
                "tokenize": tokenize,
            }
        )
        text = "<system>default</system>"
        for message in messages:
            text += f"<{message['role']}>{message['content']}</{message['role']}>"
        return text + ("<assistant>" if add_generation_prompt else "")

    def __call__(self, text, return_tensors=None, add_special_tokens=True):  # noqa: ANN001
        ids = ([EOS] if add_special_tokens else []) + char_ids(text)
        self.encode_calls.append(
            {"text": text, "ids": ids, "add_special_tokens": add_special_tokens}
        )
        return FakeEncoding(input_ids=FakeTensor(ids), attention_mask=FakeTensor([1] * len(ids)))

    def decode(self, ids, skip_special_tokens=False):  # noqa: ANN001
        return "".join(
            chr(i - OFFSET) if i >= OFFSET else ("" if skip_special_tokens else f"<{i}>")
            for i in ids
        )


class FakeModel:
    device = "cuda:0"

    def __init__(self, runtime: FakeRuntime) -> None:
        self._runtime = runtime
        self.config = SimpleNamespace(max_position_embeddings=32768)
        self.generation_config = FakeConfig(
            do_sample=True,
            temperature=0.7,
            top_p=0.8,
            top_k=20,
            repetition_penalty=1.05,
            bos_token_id=None,
            eos_token_id=[IM_END, PAD + 3],
        )
        self.generate_calls: list[dict] = []

    def eval(self) -> FakeModel:
        return self

    def get_memory_footprint(self) -> int:
        return 1917 * MIB

    def generate(self, **kwargs: Any) -> FakeTensor:
        self._runtime.events.append("generate")
        self.generate_calls.append(kwargs)
        if self._runtime.generate_error is not None:
            raise self._runtime.generate_error
        return FakeTensor(kwargs["input_ids"].row + list(self._runtime.new_tokens))


class FakeOutOfMemoryError(RuntimeError):
    pass


class FakeRuntime:
    """Fake torch and transformers sharing one record of what happened."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.cuda_available = True
        self.load_error: BaseException | None = None
        self.generate_error: BaseException | None = None
        self.new_tokens: list[int] = char_ids("Hello") + [IM_END]
        self.allocated = 100 * MIB
        self.tokenizer = FakeTokenizer()
        self.model_ref: weakref.ref | None = None
        self.bnb_configs: list[FakeConfig] = []
        self.generation_configs: list[FakeConfig] = []
        self.from_pretrained_kwargs: dict | None = None
        self.seeds: list[int] = []
        self.torch = self._make_torch()
        self.transformers = self._make_transformers()

    def model(self) -> FakeModel | None:
        return self.model_ref() if self.model_ref is not None else None

    def _make_torch(self) -> SimpleNamespace:
        runtime = self

        def empty_cache() -> None:
            runtime.events.append(("empty_cache", runtime.model() is not None))
            runtime.allocated = 108 * MIB

        @contextmanager
        def inference_mode():
            runtime.events.append("inference_mode")
            yield

        cuda = SimpleNamespace(
            is_available=lambda: runtime.cuda_available,
            empty_cache=empty_cache,
            memory_allocated=lambda: runtime.allocated,
            memory_reserved=lambda: runtime.allocated + 20 * MIB,
            OutOfMemoryError=FakeOutOfMemoryError,
        )
        return SimpleNamespace(
            cuda=cuda,
            bfloat16="torch.bfloat16",
            float16="torch.float16",
            inference_mode=inference_mode,
        )

    def _make_transformers(self) -> SimpleNamespace:
        runtime = self

        def tokenizer_from_pretrained(name: str) -> FakeTokenizer:
            runtime.events.append(("tokenizer", name))
            return runtime.tokenizer

        def model_from_pretrained(name: str, **kwargs: Any) -> FakeModel:
            runtime.events.append(("model", name))
            runtime.from_pretrained_kwargs = kwargs
            if runtime.load_error is not None:
                raise runtime.load_error
            model = FakeModel(runtime)
            runtime.model_ref = weakref.ref(model)
            runtime.allocated = 2100 * MIB
            return model

        def bnb_config(**kwargs: Any) -> FakeConfig:
            config = FakeConfig(**kwargs)
            runtime.bnb_configs.append(config)
            return config

        def generation_config(**kwargs: Any) -> FakeConfig:
            config = FakeConfig(**kwargs)
            runtime.generation_configs.append(config)
            return config

        def set_seed(seed: int) -> None:
            runtime.events.append(("set_seed", seed))
            runtime.seeds.append(seed)

        return SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer_from_pretrained),
            AutoModelForCausalLM=SimpleNamespace(from_pretrained=model_from_pretrained),
            BitsAndBytesConfig=bnb_config,
            GenerationConfig=generation_config,
            set_seed=set_seed,
        )


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    fake = FakeRuntime()
    monkeypatch.setattr(tb, "_load_runtime", lambda: (fake.torch, fake.transformers))
    return fake


def params(**overrides: Any) -> GenerationParams:
    values = {"max_new_tokens": 64, "seed": None}
    values.update(overrides)
    return GenerationParams(**values)


def empty_cache_calls(runtime: FakeRuntime) -> list[bool]:
    """For each empty_cache() call: whether the model was still alive then."""
    return [e[1] for e in runtime.events if isinstance(e, tuple) and e[0] == "empty_cache"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_unknown_llm_backend_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="Unknown LLM backend 'nope'.*stub, transformers"):
        LLMRegistry(LLMSettings(backend="nope", model_name=MODEL))


def test_get_backend_returns_a_fresh_instance() -> None:
    registry = LLMRegistry(LLMSettings(backend="stub", model_name=MODEL))

    first, second = registry.get_backend(), registry.get_backend()

    assert isinstance(first, StubLLM) and isinstance(second, StubLLM)
    assert first is not second
    assert first.get_model_info()["model_name"] == STUB_MODEL_NAME_PREFIX + MODEL


def test_registry_selects_the_transformers_backend_by_default() -> None:
    registry = LLMRegistry(LLMSettings(model_name=MODEL))

    backend = registry.get_backend()

    assert registry.backend_id == "transformers"
    assert isinstance(backend, TransformersBackend)
    assert not backend.is_loaded
    assert backend.get_model_info()["model_name"] == MODEL


def _run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120
    )


def test_both_llm_backends_are_registered() -> None:
    """In a fresh interpreter, importing the registry alone registers both backends."""
    proc = _run_python(
        "from arip.backends.llm.registry import LLMRegistry\n"
        "from arip.config import LLMSettings\n"
        "r = LLMRegistry(LLMSettings(backend='stub', model_name='m'))\n"
        "print(','.join(r.available_backends))\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "stub,transformers"


def test_llm_package_imports_without_torch() -> None:
    """torch and transformers blocked: the package must still import (llm extra is optional)."""
    proc = _run_python(
        "import sys\n"
        "sys.modules['torch'] = None\n"
        "sys.modules['transformers'] = None\n"
        "import arip.backends.llm.registry\n"
        "print('imported')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "imported"


# ---------------------------------------------------------------------------
# StubLLM
# ---------------------------------------------------------------------------


def test_stub_generate_outside_context_raises() -> None:
    with pytest.raises(RuntimeError, match="outside its context manager"):
        StubLLM().generate("prompt", params())


def test_stub_is_loaded_lifecycle() -> None:
    stub = StubLLM()
    assert not stub.is_loaded

    with stub as entered:
        assert entered is stub
        assert stub.is_loaded

    assert not stub.is_loaded
    with pytest.raises(RuntimeError):
        stub.generate("prompt", params())


def test_stub_returns_responses_in_order_and_repeats_the_last() -> None:
    with StubLLM(responses=["first answer", "second"], stop_reason="max_tokens") as stub:
        results = [stub.generate("one two three", params()) for _ in range(3)]

    assert [r.text for r in results] == ["first answer", "second", "second"]
    assert {r.stop_reason for r in results} == {"max_tokens"}
    assert results[0].prompt_tokens == 3
    assert results[0].completion_tokens == 2


def test_stub_returns_a_configured_generation_result_unchanged() -> None:
    canned = GenerationResult(
        text="cut", prompt_tokens=7, completion_tokens=1, latency_ms=5, stop_reason="max_tokens"
    )
    with StubLLM(responses=[canned]) as stub:
        assert stub.generate("p", params()) is canned


def test_stub_raises_the_configured_error_on_chosen_calls_and_records_every_call() -> None:
    error = LLMError("boom")
    first, second = params(seed=1), params(seed=2)
    with StubLLM(fail_on_calls=[2], error=error) as stub:
        stub.generate("a", first)
        with pytest.raises(LLMError) as raised:
            stub.generate("b", second)
        stub.generate("c", first)

    assert raised.value is error
    assert stub.calls == [("a", first), ("b", second), ("c", first)]


def test_stub_default_error_is_an_llm_error() -> None:
    with StubLLM(fail_on_calls=[1]) as stub, pytest.raises(LLMError, match="call 1"):
        stub.generate("a", params())


def test_stub_model_info_is_marked() -> None:
    assert StubLLM(model_name=MODEL).get_model_info() == {
        "model_name": "stub:" + MODEL,
        "context_length": None,
        "quantization": None,
    }


# ---------------------------------------------------------------------------
# TransformersBackend — loading
# ---------------------------------------------------------------------------


def test_quantization_config_is_nf4_bfloat16_without_double_quant(runtime: FakeRuntime) -> None:
    with TransformersBackend(MODEL):
        pass

    assert [c.kwargs for c in runtime.bnb_configs] == [
        {
            "load_in_4bit": True,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_compute_dtype": "torch.bfloat16",
            "bnb_4bit_use_double_quant": False,
        }
    ]


def test_from_pretrained_receives_quantization_config(runtime: FakeRuntime) -> None:
    with TransformersBackend(MODEL):
        pass

    kwargs = runtime.from_pretrained_kwargs
    assert kwargs is not None
    assert kwargs.get("quantization_config") is runtime.bnb_configs[0]
    assert kwargs.get("dtype") == "torch.bfloat16"
    assert "load_in_4bit" not in kwargs


def test_model_is_placed_on_cuda_0_only(runtime: FakeRuntime) -> None:
    with TransformersBackend(MODEL):
        pass

    assert runtime.from_pretrained_kwargs["device_map"] == {"": 0}


def test_enter_without_cuda_raises_llm_error(runtime: FakeRuntime) -> None:
    runtime.cuda_available = False
    backend = TransformersBackend(MODEL)

    with pytest.raises(LLMError, match="GPU-only"):
        backend.__enter__()

    assert not backend.is_loaded
    assert runtime.from_pretrained_kwargs is None


def test_oom_in_enter_raises_llm_error_with_sds_message(runtime: FakeRuntime) -> None:
    oom = FakeOutOfMemoryError("CUDA out of memory")
    runtime.load_error = oom
    backend = TransformersBackend(MODEL)

    with pytest.raises(LLMError) as raised:
        backend.__enter__()

    assert str(raised.value) == SDS_OOM_MESSAGE
    assert raised.value.__cause__ is oom
    assert not backend.is_loaded


def test_oom_in_enter_cleans_up(runtime: FakeRuntime) -> None:
    runtime.load_error = FakeOutOfMemoryError("CUDA out of memory")

    with pytest.raises(LLMError):
        TransformersBackend(MODEL).__enter__()

    assert empty_cache_calls(runtime) == [False]


def test_missing_runtime_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = ImportError("No module named 'torch'")

    def no_runtime():
        raise missing

    monkeypatch.setattr(tb, "_load_runtime", no_runtime)

    with pytest.raises(LLMError, match="llm` extra") as raised:
        TransformersBackend(MODEL).__enter__()
    assert raised.value.__cause__ is missing


def test_load_failure_raises_chained_llm_error(runtime: FakeRuntime) -> None:
    failure = ImportError("bitsandbytes is required for 4-bit quantization")
    runtime.load_error = failure
    backend = TransformersBackend(MODEL)

    with pytest.raises(LLMError, match="Could not load LLM") as raised:
        backend.__enter__()

    assert raised.value.__cause__ is failure
    assert not backend.is_loaded


def test_model_generation_defaults_are_replaced_at_load(runtime: FakeRuntime) -> None:
    with capture_logs() as logs, TransformersBackend(MODEL):
        model = runtime.model()
        assert model is not None
        replaced = model.generation_config.kwargs
        del model

    assert replaced == {"bos_token_id": None, "eos_token_id": [IM_END, 3], "pad_token_id": PAD}
    loaded = next(e for e in logs if e["event"] == "llm_loaded")
    assert loaded["dropped_generation_defaults"] == {
        "do_sample": True,
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "repetition_penalty": 1.05,
    }


# ---------------------------------------------------------------------------
# TransformersBackend — generation
# ---------------------------------------------------------------------------


def test_prompt_is_wrapped_as_one_user_message(runtime: FakeRuntime) -> None:
    with TransformersBackend(MODEL) as llm:
        llm.generate("Explain {braces}.", params())

    tokenizer = runtime.tokenizer
    assert tokenizer.template_calls == [
        {
            "messages": [{"role": "user", "content": "Explain {braces}."}],
            "add_generation_prompt": True,
            "tokenize": False,
        }
    ]
    expected = tokenizer.apply_chat_template(
        [{"role": "user", "content": "Explain {braces}."}], add_generation_prompt=True
    )
    assert [c["text"] for c in tokenizer.encode_calls] == [expected]
    assert tokenizer.encode_calls[0]["add_special_tokens"] is False


def test_text_holds_only_new_tokens(runtime: FakeRuntime) -> None:
    runtime.new_tokens = char_ids("Only this.") + [IM_END]

    with TransformersBackend(MODEL) as llm:
        result = llm.generate("A prompt that must not be echoed", params())

    assert result.text == "Only this."


def test_token_counts(runtime: FakeRuntime) -> None:
    runtime.new_tokens = char_ids("abc") + [IM_END]

    with TransformersBackend(MODEL) as llm:
        result = llm.generate("prompt", params())

    assert result.prompt_tokens == len(runtime.tokenizer.encode_calls[0]["ids"])
    assert result.prompt_tokens > len("prompt")
    assert result.completion_tokens == 4


def test_stop_reason_max_tokens(runtime: FakeRuntime) -> None:
    runtime.new_tokens = char_ids("abcd")

    with TransformersBackend(MODEL) as llm:
        result = llm.generate("p", params(max_new_tokens=4))

    assert result.stop_reason == "max_tokens"


def test_stop_reason_eos(runtime: FakeRuntime) -> None:
    """eos wins even when the eos token is also the last allowed token."""
    runtime.new_tokens = char_ids("abc") + [IM_END]

    with TransformersBackend(MODEL) as llm:
        result = llm.generate("p", params(max_new_tokens=4))

    assert result.stop_reason == "eos"


def test_stop_reason_stop_sequence(runtime: FakeRuntime) -> None:
    """stop_sequence wins over max_tokens, and the stop string stays in the text."""
    runtime.new_tokens = char_ids("abEND")

    with TransformersBackend(MODEL) as llm:
        result = llm.generate("p", params(max_new_tokens=5, stop_sequences=["END"]))

    assert result.stop_reason == "stop_sequence"
    assert result.text == "abEND"


def test_unknown_stop_reason_raises_llm_error(runtime: FakeRuntime) -> None:
    runtime.new_tokens = char_ids("ab")

    with TransformersBackend(MODEL) as llm, pytest.raises(LLMError, match="stop reason"):
        llm.generate("p", params(max_new_tokens=5))


def test_stop_sequences_reach_generate(runtime: FakeRuntime) -> None:
    with TransformersBackend(MODEL) as llm:
        llm.generate("p", params(stop_sequences=["END", "###"]))
        llm.generate("p", params())
        model = runtime.model()
        with_stops, without = model.generate_calls
        del model

    with_config, without_config = runtime.generation_configs[-2:]
    assert with_config.kwargs["stop_strings"] == ["END", "###"]
    assert with_stops["tokenizer"] is runtime.tokenizer
    assert without_config.kwargs["stop_strings"] is None
    assert "tokenizer" not in without


def test_seed_is_applied_before_generate(runtime: FakeRuntime) -> None:
    with TransformersBackend(MODEL) as llm:
        llm.generate("p", params(seed=42))
        llm.generate("p", params(seed=None))

    calls = [
        e
        for e in runtime.events
        if e == "generate" or (isinstance(e, tuple) and e[0] == "set_seed")
    ]
    assert calls == [("set_seed", 42), "generate", "generate"]


def test_generation_config_is_fully_specified(runtime: FakeRuntime) -> None:
    with TransformersBackend(MODEL) as llm:
        llm.generate("p", GenerationParams())
        model = runtime.model()
        call = model.generate_calls[0]
        del model

    config = runtime.generation_configs[-1]
    assert call["generation_config"] is config
    assert config.kwargs == {
        "max_new_tokens": 1024,
        "do_sample": True,
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 0,
        "repetition_penalty": 1.1,
        "stop_strings": None,
        "eos_token_id": [IM_END, 3],
        "pad_token_id": PAD,
    }
    assert "inference_mode" in runtime.events


def test_zero_temperature_is_greedy(runtime: FakeRuntime) -> None:
    with TransformersBackend(MODEL) as llm:
        llm.generate("p", params(temperature=0.0))

    config = runtime.generation_configs[-1].kwargs
    assert (config["do_sample"], config["temperature"], config["top_p"]) == (False, 1.0, 1.0)


def test_inference_error_raises_llm_error(runtime: FakeRuntime) -> None:
    failure = FakeOutOfMemoryError("CUDA out of memory during generate")
    runtime.generate_error = failure

    with TransformersBackend(MODEL) as llm:
        with pytest.raises(LLMError, match="Generation failed") as raised:
            llm.generate("p", params())
        assert raised.value.__cause__ is failure
        assert llm.is_loaded


def test_generate_before_enter_raises_runtime_error(runtime: FakeRuntime) -> None:
    with pytest.raises(RuntimeError, match="outside its context manager"):
        TransformersBackend(MODEL).generate("p", params())


# ---------------------------------------------------------------------------
# TransformersBackend — unloading and model info
# ---------------------------------------------------------------------------


def test_exit_drops_model_then_empties_cache(runtime: FakeRuntime) -> None:
    with TransformersBackend(MODEL):
        pass

    assert empty_cache_calls(runtime) == [False]


def test_exit_releases_every_model_reference(runtime: FakeRuntime) -> None:
    """The backend stays referenced, so a reference it kept would keep the model alive."""
    backend = TransformersBackend(MODEL)
    with backend:
        assert runtime.model() is not None

    assert runtime.model() is None


def test_is_loaded_false_after_exit(runtime: FakeRuntime) -> None:
    backend = TransformersBackend(MODEL)
    assert not backend.is_loaded

    with backend:
        assert backend.is_loaded

    assert not backend.is_loaded


def test_exit_unloads_when_body_raises(runtime: FakeRuntime) -> None:
    backend = TransformersBackend(MODEL)

    with pytest.raises(ValueError, match="body failed"), backend:
        raise ValueError("body failed")

    assert not backend.is_loaded
    assert runtime.model() is None
    assert empty_cache_calls(runtime) == [False]


def test_exit_does_not_swallow_exception(runtime: FakeRuntime) -> None:
    backend = TransformersBackend(MODEL)
    backend.__enter__()

    assert backend.__exit__(ValueError, ValueError("x"), None) is None


def test_unload_logs_memory(runtime: FakeRuntime) -> None:
    with capture_logs() as logs, TransformersBackend(MODEL):
        pass

    events = {e["event"]: e for e in logs}
    assert (events["llm_loading"]["allocated_mib"], events["llm_loading"]["reserved_mib"]) == (
        100.0,
        120.0,
    )
    unloaded = events["llm_unloaded"]
    assert (unloaded["allocated_mib"], unloaded["reserved_mib"]) == (108.0, 128.0)
    assert unloaded["allocated_before_load_mib"] == 100.0
    assert unloaded["allocated_delta_mib"] == 8.0


def test_model_info_reports_quantization(runtime: FakeRuntime) -> None:
    backend = TransformersBackend(MODEL)
    expected = {
        "model_name": MODEL,
        "context_length": None,
        "quantization": "bitsandbytes-nf4",
        "compute_dtype": "bfloat16",
        "double_quant": False,
        "chat_template_applied": True,
    }
    assert backend.get_model_info() == expected

    with backend:
        assert backend.get_model_info() == {**expected, "context_length": 32768}


# ---------------------------------------------------------------------------
# Shipped configuration (D-008)
# ---------------------------------------------------------------------------


def test_shipped_settings_use_the_d008_model() -> None:
    """Parsed directly: load_settings() would also read .env and environment variables."""
    data = yaml.safe_load((REPO / "config" / "settings.yaml").read_text(encoding="utf-8"))
    llm = LLMSettings(**data["llm"])

    assert llm.backend == "transformers"
    assert llm.model_name == "Qwen/Qwen2.5-3B-Instruct"
