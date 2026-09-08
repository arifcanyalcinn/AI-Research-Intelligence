"""
Unit tests for the embedding backends and their registry.

Covers SDS §5.6 (embedding service, CPU-only, context-manager lifecycle) and
the plugin discovery rule of §1.2 / AD-02 / D-004.

No model is ever loaded. `StubEmbedder` loads nothing by design, and
`SentenceTransformersBackend` is tested against a fake `sentence_transformers`
module injected into `sys.modules` — so these tests neither download the ~90 MB
all-MiniLM-L6-v2 model nor require `sentence-transformers` to be installed.
That matches §5.6's testing strategy ("without loading any real model") and
keeps the suite runnable on a `[dev]`-only install of everything except numpy.

numpy is required, because the backends return arrays. It arrives with the
optional `embedding` extra, so the module skips rather than fails when only
`[dev]` is installed — see the importorskip below.
"""

from __future__ import annotations

import sys
import types

import pytest

np = pytest.importorskip(
    "numpy",
    reason="numpy arrives with the optional `embedding` extra; "
    "install with `pip install -e \".[embedding]\"` to run these tests",
)

from arip.backends.embeddings import sentence_transformers_backend as st_module  # noqa: E402
from arip.backends.embeddings import stub_backend as stub_module  # noqa: E402
from arip.backends.embeddings.registry import EmbeddingRegistry  # noqa: E402
from arip.backends.embeddings.sentence_transformers_backend import (  # noqa: E402
    EMBEDDING_DEVICE,
    SentenceTransformersBackend,
)
from arip.backends.embeddings.stub_backend import (  # noqa: E402
    STUB_EMBEDDING_DIM,
    STUB_MODEL_NAME_PREFIX,
    StubEmbedder,
)
from arip.config import EmbeddingSettings  # noqa: E402
from arip.exceptions import ConfigError, EmbeddingError  # noqa: E402
from arip.interfaces import BaseEmbedder  # noqa: E402

# ---------------------------------------------------------------------------
# Fake sentence_transformers — never touches the network or a model file
# ---------------------------------------------------------------------------


class _FakeSentenceTransformer:
    """Records how it was constructed and returns canned vectors."""

    last_init: tuple[str, str] | None = None

    def __init__(self, model_name: str, device: str | None = None) -> None:
        type(self).last_init = (model_name, device or "")
        self.model_name = model_name
        self.device = device
        self.encode_calls: list[list[str]] = []
        self.raise_on_encode: Exception | None = None

    def get_sentence_embedding_dimension(self) -> int:
        return 384

    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False):  # noqa: ANN001
        self.encode_calls.append(list(texts))
        if self.raise_on_encode is not None:
            raise self.raise_on_encode
        return np.ones((len(texts), 384), dtype=np.float64)


@pytest.fixture
def fake_st(monkeypatch: pytest.MonkeyPatch):
    """Install a fake `sentence_transformers` module for the duration of a test."""
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    _FakeSentenceTransformer.last_init = None
    return _FakeSentenceTransformer


@pytest.fixture
def failing_st(monkeypatch: pytest.MonkeyPatch):
    """Install a `sentence_transformers` whose constructor raises."""

    class _Exploding:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise OSError("could not reach huggingface.co")

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _Exploding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)


def settings(**overrides: object) -> EmbeddingSettings:
    base: dict[str, object] = {"backend": "stub", "model_name": "all-MiniLM-L6-v2"}
    base.update(overrides)
    return EmbeddingSettings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Optional-dependency discipline
# ---------------------------------------------------------------------------


def test_backend_modules_do_not_import_numpy_at_module_level() -> None:
    """numpy must not be a module-level name in either backend.

    Both modules are imported by the package __init__ (AD-02), which runs
    whenever anything touches the registry. A module-level numpy import would
    make the optional `embedding` extra mandatory.
    """
    assert "np" not in vars(stub_module)
    assert "numpy" not in vars(stub_module)
    assert "np" not in vars(st_module)
    assert "numpy" not in vars(st_module)


def test_sentence_transformers_is_not_imported_at_module_level() -> None:
    """The heavy import lives inside __enter__, not at module scope."""
    assert "SentenceTransformer" not in vars(st_module)
    assert "sentence_transformers" not in vars(st_module)


def test_registry_discovers_backends_without_sentence_transformers_installed() -> None:
    """Discovery must not depend on the optional dependency being present.

    This is the property that lets `pip install -e .` (no extra) still import
    the package: registration only needs the class objects, not the library.
    """
    registry = EmbeddingRegistry(settings(backend="stub"))

    assert "sentence_transformers" in registry.available_backends
    assert "stub" in registry.available_backends


# ---------------------------------------------------------------------------
# Plugin discovery — SDS §1.2, AD-02, D-004
# ---------------------------------------------------------------------------


def test_both_backends_are_direct_subclasses_of_base_embedder() -> None:
    """D-004: __subclasses__() does not recurse, so no intermediate base.

    An intermediate class between BaseEmbedder and these two would make
    __subclasses__() yield the intermediate — which has no backend_id — and
    both real backends would vanish from the registry.
    """
    direct = set(BaseEmbedder.__subclasses__())

    assert StubEmbedder in direct
    assert SentenceTransformersBackend in direct
    assert StubEmbedder.__bases__ == (BaseEmbedder,)
    assert SentenceTransformersBackend.__bases__ == (BaseEmbedder,)


def test_backend_ids_are_unique() -> None:
    """Two backends sharing an id would silently shadow one another."""
    ids = [cls.backend_id for cls in BaseEmbedder.__subclasses__()]

    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Registry selection
# ---------------------------------------------------------------------------


def test_registry_selects_the_configured_backend() -> None:
    registry = EmbeddingRegistry(settings(backend="stub"))

    assert registry.backend_id == "stub"
    assert isinstance(registry.get_backend(), StubEmbedder)


def test_registry_selects_the_real_backend_when_configured() -> None:
    """Selecting it constructs nothing heavy — the model loads on __enter__."""
    registry = EmbeddingRegistry(settings(backend="sentence_transformers"))

    assert isinstance(registry.get_backend(), SentenceTransformersBackend)


def test_unknown_backend_raises_config_error() -> None:
    """§5.15: invalid configuration is fatal at startup, not at first use."""
    with pytest.raises(ConfigError) as excinfo:
        EmbeddingRegistry(settings(backend="does_not_exist"))

    assert "does_not_exist" in str(excinfo.value)


def test_unknown_backend_message_lists_the_alternatives() -> None:
    """The error has to be actionable without reading the source."""
    with pytest.raises(ConfigError) as excinfo:
        EmbeddingRegistry(settings(backend="typo"))

    message = str(excinfo.value)
    assert "stub" in message
    assert "sentence_transformers" in message


def test_registry_passes_the_configured_model_name() -> None:
    """The registry overrides every backend's default with config.model_name.

    This is why the stub cannot rely on a default to mark itself: the value it
    is constructed with is always the configured one. It marks on the way out
    instead — see the stub model-name tests below.
    """
    registry = EmbeddingRegistry(settings(backend="stub", model_name="custom-model"))

    assert registry.get_backend().model_name == "stub:custom-model"


def test_get_backend_returns_a_fresh_instance_each_call() -> None:
    """A shared instance would be left unloaded by a previous __exit__."""
    registry = EmbeddingRegistry(settings(backend="stub"))

    assert registry.get_backend() is not registry.get_backend()


# ---------------------------------------------------------------------------
# StubEmbedder — SDS §5.6 testing strategy
# ---------------------------------------------------------------------------


def test_stub_reports_the_documented_dimension() -> None:
    """384, matching all-MiniLM-L6-v2, so index width is backend-independent."""
    assert StubEmbedder().embedding_dim == STUB_EMBEDDING_DIM == 384


def test_stub_returns_one_row_per_text() -> None:
    with StubEmbedder() as embedder:
        vectors = embedder.embed(["one", "two", "three"])

    assert vectors.shape == (3, 384)


def test_stub_returns_float32() -> None:
    with StubEmbedder() as embedder:
        assert embedder.embed(["x"]).dtype == np.float32


def test_stub_returns_unit_vectors() -> None:
    """§5.6: "random unit vectors of the correct dimension"."""
    with StubEmbedder() as embedder:
        vectors = embedder.embed(["alpha", "beta", "gamma"])

    norms = np.linalg.norm(vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-6)


def test_stub_is_deterministic_for_the_same_text() -> None:
    """Identical text must give an identical vector.

    Without this, no test could assert that two items with the same text are
    semantic duplicates — the behaviour §5.7's testing strategy requires.
    """
    with StubEmbedder() as embedder:
        first = embedder.embed(["the same text"])
        second = embedder.embed(["the same text"])

    assert np.array_equal(first, second)


def test_stub_is_deterministic_across_instances() -> None:
    """Determinism survives a reload, so it survives a process restart."""
    with StubEmbedder() as a:
        first = a.embed(["stable"])
    with StubEmbedder() as b:
        second = b.embed(["stable"])

    assert np.array_equal(first, second)


def test_stub_gives_different_vectors_for_different_text() -> None:
    with StubEmbedder() as embedder:
        vectors = embedder.embed(["completely different", "not at all alike"])

    similarity = float(np.dot(vectors[0], vectors[1]))
    assert similarity < 0.5


def test_stub_empty_input_returns_empty_array() -> None:
    """An empty batch is not an error."""
    with StubEmbedder() as embedder:
        vectors = embedder.embed([])

    assert vectors.shape == (0, 384)


def test_stub_embed_outside_context_manager_raises() -> None:
    """§5.6 contract: the model must be loaded before embed()."""
    with pytest.raises(RuntimeError, match="context manager"):
        StubEmbedder().embed(["x"])


def test_stub_embed_after_exit_raises() -> None:
    """__exit__ genuinely unloads, rather than leaving a usable object."""
    embedder = StubEmbedder()
    with embedder:
        embedder.embed(["x"])

    with pytest.raises(RuntimeError):
        embedder.embed(["x"])


def test_stub_context_manager_returns_itself() -> None:
    embedder = StubEmbedder()
    with embedder as entered:
        assert entered is embedder


def test_stub_exit_runs_even_when_the_body_raises() -> None:
    """§5.6 chose a context manager precisely to guarantee this."""
    embedder = StubEmbedder()
    with pytest.raises(ValueError), embedder:
        raise ValueError("boom")

    with pytest.raises(RuntimeError):
        embedder.embed(["x"])


def test_stub_marks_its_model_name_on_the_production_path() -> None:
    """A stub-embedded row must be identifiable in the database.

    Constructed the way production constructs it — through the registry, with
    ``embeddings.backend: "stub"`` and a real model name in config. Asserting
    on a directly-constructed ``StubEmbedder()`` would test a path the
    application never takes, because the registry always overrides the default
    with ``config.model_name``.

    Stage 4 writes this value to ``items.embedding_model_name``. §5.7's
    reconciliation selects on ``embedding_computed_at IS NOT NULL`` and cannot
    separate stub rows from real ones; this prefix is what makes that possible.
    """
    registry = EmbeddingRegistry(settings(backend="stub", model_name="all-MiniLM-L6-v2"))

    recorded = registry.get_backend().model_name

    assert recorded.startswith(STUB_MODEL_NAME_PREFIX)
    assert recorded == "stub:all-MiniLM-L6-v2"


def test_stub_and_real_model_names_cannot_be_confused() -> None:
    """The same config must produce different recorded names per backend.

    The drift guard: if the stub ever loses its prefix, or the real backend
    ever gains one, this fails. Both registries differ only in `backend`.
    """
    config = {"model_name": "all-MiniLM-L6-v2"}
    stub_name = EmbeddingRegistry(settings(backend="stub", **config)).get_backend().model_name
    real_name = (
        EmbeddingRegistry(settings(backend="sentence_transformers", **config))
        .get_backend()
        .model_name
    )

    assert stub_name != real_name
    assert stub_name.startswith(STUB_MODEL_NAME_PREFIX)
    assert not real_name.startswith(STUB_MODEL_NAME_PREFIX)
    assert real_name == "all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# SentenceTransformersBackend — SDS §5.6
# ---------------------------------------------------------------------------


def test_device_is_cpu(fake_st) -> None:
    """§5.6: CPU-only "enforced by ... explicit code", and AD-06.

    This is the assertion that keeps the embedder off the GPU the LLM owns.
    """
    with SentenceTransformersBackend("all-MiniLM-L6-v2"):
        pass

    assert fake_st.last_init == ("all-MiniLM-L6-v2", "cpu")
    assert EMBEDDING_DEVICE == "cpu"


def test_configured_model_name_is_loaded(fake_st) -> None:
    with SentenceTransformersBackend("some-other-model"):
        pass

    assert fake_st.last_init[0] == "some-other-model"


def test_model_is_not_loaded_before_enter(fake_st) -> None:
    """Lazy load (§5.6): constructing the backend touches no model."""
    SentenceTransformersBackend("all-MiniLM-L6-v2")

    assert fake_st.last_init is None


def test_embedding_dim_comes_from_the_model(fake_st) -> None:
    with SentenceTransformersBackend("all-MiniLM-L6-v2") as embedder:
        assert embedder.embedding_dim == 384


def test_embedding_dim_before_load_raises(fake_st) -> None:
    """The dimension is a property of the model, unknowable before loading."""
    with pytest.raises(RuntimeError, match="not loaded|Use `with"):
        _ = SentenceTransformersBackend("all-MiniLM-L6-v2").embedding_dim


def test_load_failure_raises_embedding_error(failing_st) -> None:
    """§5.6 failure mode: model download failure -> EmbeddingError."""
    with pytest.raises(EmbeddingError), SentenceTransformersBackend("all-MiniLM-L6-v2"):
        pass


def test_load_failure_message_tells_the_operator_to_pre_download(failing_st) -> None:
    """§5.6: "inform user to pre-download model"."""
    with pytest.raises(EmbeddingError) as excinfo:
        with SentenceTransformersBackend("all-MiniLM-L6-v2"):
            pass

    message = str(excinfo.value)
    assert "pre-download" in message
    assert "all-MiniLM-L6-v2" in message


def test_embed_returns_float32(fake_st) -> None:
    """The model may return float64; the contract says float32."""
    with SentenceTransformersBackend("all-MiniLM-L6-v2") as embedder:
        vectors = embedder.embed(["a", "b"])

    assert vectors.dtype == np.float32
    assert vectors.shape == (2, 384)


def test_embed_passes_every_text_to_the_model(fake_st) -> None:
    with SentenceTransformersBackend("all-MiniLM-L6-v2") as embedder:
        embedder.embed(["first", "second"])
        model = embedder._model  # noqa: SLF001 - asserting the call reached the model

    assert model.encode_calls == [["first", "second"]]


def test_embed_empty_input_does_not_call_the_model(fake_st) -> None:
    with SentenceTransformersBackend("all-MiniLM-L6-v2") as embedder:
        vectors = embedder.embed([])
        model = embedder._model  # noqa: SLF001

    assert vectors.shape == (0, 384)
    assert model.encode_calls == []


def test_embed_outside_context_manager_raises(fake_st) -> None:
    with pytest.raises(RuntimeError, match="context manager"):
        SentenceTransformersBackend("all-MiniLM-L6-v2").embed(["x"])


def test_exit_unloads_the_model(fake_st) -> None:
    """§5.6: unload after the pass, releasing CPU RAM."""
    embedder = SentenceTransformersBackend("all-MiniLM-L6-v2")
    with embedder:
        pass

    assert embedder._model is None  # noqa: SLF001


def test_encode_failure_raises_embedding_error(fake_st) -> None:
    with SentenceTransformersBackend("all-MiniLM-L6-v2") as embedder:
        embedder._model.raise_on_encode = RuntimeError("kernel exploded")  # noqa: SLF001
        with pytest.raises(EmbeddingError, match="Embedding failed"):
            embedder.embed(["x"])


def test_memory_error_raises_embedding_error(fake_st) -> None:
    """§5.6 failure mode: OOM on CPU RAM -> EmbeddingError."""
    with SentenceTransformersBackend("all-MiniLM-L6-v2") as embedder:
        embedder._model.raise_on_encode = MemoryError()  # noqa: SLF001
        with pytest.raises(EmbeddingError, match="CPU RAM"):
            embedder.embed(["x"])


def test_real_backend_exposes_the_configured_model_name_unprefixed(fake_st) -> None:
    """This is the value Stage 4 writes to items.embedding_model_name.

    It must be the bare model name — the ``stub:`` prefix belongs to the stub
    alone, and a real row that carried it would be as misleading as a stub row
    that did not.
    """
    assert SentenceTransformersBackend("all-MiniLM-L6-v2").model_name == "all-MiniLM-L6-v2"
    assert SentenceTransformersBackend("other").model_name == "other"
    assert not SentenceTransformersBackend("all-MiniLM-L6-v2").model_name.startswith(
        STUB_MODEL_NAME_PREFIX
    )


def test_backend_ids_match_the_configuration_defaults() -> None:
    """config.embeddings.backend defaults to the real backend's id."""
    assert SentenceTransformersBackend.backend_id == EmbeddingSettings().backend
    assert StubEmbedder.backend_id == "stub"
