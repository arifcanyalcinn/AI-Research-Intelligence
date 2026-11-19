"""
GPU integration test of TransformersBackend (SDS §5.9, D-012, ruling A17).

§5.9: "The real backend is integration-tested separately against a small
model (~1B parameters)." D-012 departs from "~1B": the test loads the
production model named in ``config/settings.yaml`` (D-008), which is already
in the local Hugging Face cache, so it tests the exact shipped configuration
and never downloads.

Not part of the default gate. It runs only with ``--run-gpu`` (ruling Q8), and
then skips unless all of these hold:

* torch, transformers and bitsandbytes are importable;
* CUDA is available;
* ``HF_HUB_OFFLINE=1`` is set before the run starts (huggingface_hub reads it
  at import time, so a run can never reach the Hub);
* the model is in the local cache.

``config/settings.yaml`` is parsed directly rather than through
``load_settings()``, which would also read ``.env``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from arip.backends.llm.registry import LLMRegistry
from arip.config import LLMSettings
from arip.entities import GenerationParams

pytestmark = pytest.mark.gpu

SETTINGS = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
MIB = 1024 * 1024
UNLOAD_TOLERANCE_MIB = 16
"""Stage 1 measured an 8.1 MiB residue after unloading: the cuBLAS workspace."""


def shipped_llm_settings() -> LLMSettings:
    data = yaml.safe_load(SETTINGS.read_text(encoding="utf-8"))
    return LLMSettings(**data["llm"])


def four_bit_layers(model) -> tuple[int, set[str]]:
    """Count the Linear4bit layers and collect their quant types.

    Returns plain values only, never the modules. A module reference that
    outlives the `with` block keeps its 4-bit weights on the GPU after
    __exit__ (Stage 5 GPU run: 1500 MiB still allocated), and the release
    assertion would then measure this test instead of the backend.
    """
    count = 0
    quant_types: set[str] = set()
    for module in model.modules():
        if type(module).__name__ == "Linear4bit":
            count += 1
            quant_types.add(module.weight.quant_type)
    return count, quant_types


@pytest.fixture
def cuda_runtime():
    torch = pytest.importorskip("torch")
    pytest.importorskip("transformers")
    pytest.importorskip("bitsandbytes")
    if not torch.cuda.is_available():
        pytest.skip("CUDA is not available")
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        pytest.skip("set HF_HUB_OFFLINE=1 before the run, so the test cannot download")
    from huggingface_hub import snapshot_download

    settings = shipped_llm_settings()
    try:
        snapshot_download(settings.model_name, local_files_only=True)
    except Exception as exc:  # LocalEntryNotFoundError and relatives
        pytest.skip(f"{settings.model_name} is not in the local cache: {exc}")
    return torch, settings


def test_transformers_backend_loads_generates_and_releases_vram(cuda_runtime) -> None:
    torch, settings = cuda_runtime
    allocated_before = torch.cuda.memory_allocated()
    llm = LLMRegistry(settings).get_backend()

    with llm:
        assert llm.is_loaded
        # Private access on purpose: the 4-bit layers are the evidence of NF4.
        count, quant_types = four_bit_layers(llm._model)
        assert count, "no Linear4bit layers: the model was not loaded in 4-bit"
        assert quant_types == {"nf4"}
        info = llm.get_model_info()
        assert info["quantization"] == "bitsandbytes-nf4"
        assert info["context_length"]

        result = llm.generate(
            "In one sentence, what is a transformer model in machine learning?",
            GenerationParams(max_new_tokens=8, seed=0),
        )
        assert result.text.strip()
        assert result.stop_reason == "max_tokens"
        assert result.completion_tokens == 8
        assert result.prompt_tokens > 0

    assert not llm.is_loaded
    released = torch.cuda.memory_allocated() - allocated_before
    assert (
        released <= UNLOAD_TOLERANCE_MIB * MIB
    ), f"{released / MIB:.1f} MiB still allocated after unloading"
