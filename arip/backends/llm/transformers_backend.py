"""
TransformersBackend — the v1 GPU LLM backend (SDS §5.9, D-008, D-011).

Hugging Face ``transformers`` with bitsandbytes 4-bit NF4 quantization, used as
the §5.9 context manager: ``__enter__`` loads the model onto the GPU,
``generate()`` runs one prompt, ``__exit__`` unloads it and empties the CUDA
cache.

**GPU-only, by explicit code (AD-06).** ``__enter__`` raises ``LLMError`` when
CUDA is unavailable; there is no CPU fallback. The model is placed with
``device_map={"": 0}`` rather than ``"auto"``, because accelerate's ``"auto"``
may offload layers to the CPU when VRAM is short. That would break AD-06
quietly and turn §5.9's "VRAM insufficient" error into a slow run.

**Quantization (D-008, ruling A15).** NF4, compute dtype bfloat16, no double
quantization, non-quantized weights in bfloat16. Module constants, not
configuration, and reported by ``get_model_info()``. NF4 is stated explicitly
because ``BitsAndBytesConfig`` defaults to ``fp4``.

**Prompt wrapping (D-011, ruling A8; SDS 5.9.1, D-013).** ``generate()``
receives the Jinja2 output and wraps it as one user message with the
tokenizer's chat template (``add_generation_prompt=True``). A ``system`` text,
when given, is sent as the system message before it; without one the
tokenizer's default system message applies unmodified. ``prompt_tokens`` counts the wrapped input;
``completion_tokens`` counts only the new tokens, and ``text`` decodes only
those.

**Generation settings (D-011, rulings Q3 and Q4).** Every call gets a fully
specified ``GenerationConfig`` built only from ``GenerationParams``; top-k is
disabled (``top_k=0``). transformers 5.x fills every value left ``None`` from
the checkpoint's ``generation_config.json``, so at load the model's own
generation config is replaced by one holding only its token ids, and the
dropped values are logged. ``temperature <= 0`` means greedy decoding.

**Stop reason (D-011, ruling Q5).** Checked in the order eos, stop_sequence,
max_tokens. A matched stop string is kept in ``text``.

**Imports are function-local, deliberately.** torch and transformers arrive
only with the optional ``llm`` extra, and this module is imported by the
package ``__init__`` whenever anything touches the registry (AD-02). They are
imported in one place, ``_load_runtime()``, which is also the seam the unit
tests replace with a fake runtime.
"""

from __future__ import annotations

import gc
import time
import traceback
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from arip.entities import GenerationResult
from arip.exceptions import LLMError
from arip.interfaces import BaseLLM

if TYPE_CHECKING:
    from arip.entities import GenerationParams

logger = structlog.get_logger(__name__)

MODEL_DEVICE_MAP: dict[str, int] = {"": 0}
"""Every module on CUDA device 0. Never ``"auto"``: no CPU offload (AD-06)."""

QUANT_TYPE = "nf4"
"""bitsandbytes 4-bit quant type (D-008). The library default is ``fp4``."""

COMPUTE_DTYPE = "bfloat16"
"""bitsandbytes compute dtype and the dtype of non-quantized weights (A15)."""

DOUBLE_QUANT = False
"""Double quantization off (A15): the measured peak leaves ample VRAM."""

QUANTIZATION_LABEL = f"bitsandbytes-{QUANT_TYPE}"
"""The ``quantization`` value reported by ``get_model_info()``."""

TOP_K_DISABLED = 0
"""``top_k=0`` disables top-k filtering in transformers (ruling Q3)."""

MIB = 1024 * 1024

OOM_MESSAGE = "VRAM insufficient for {model_name}. Try a smaller or more quantized model."
"""SDS §5.9's message for ``torch.cuda.OutOfMemoryError`` in ``__enter__``."""


def _load_runtime() -> tuple[Any, Any]:
    """Import torch and transformers.

    The only place this module imports either. Raises ImportError when the
    optional ``llm`` extra is not installed; ``__enter__`` converts that.
    """
    import torch  # noqa: PLC0415 - optional dependency, see module docstring
    import transformers  # noqa: PLC0415 - optional dependency, see module docstring

    return torch, transformers


class TransformersBackend(BaseLLM):
    """4-bit NF4 inference on the GPU via ``transformers`` and bitsandbytes.

    Direct subclass of ``BaseLLM`` per D-004.
    """

    backend_id: ClassVar[str] = "transformers"

    def __init__(self, model_name: str) -> None:
        """
        Args:
            model_name: Hugging Face model id, from ``config.llm.model_name``.
        """
        self._model_name = model_name
        self._model: Any = None
        self._tokenizer: Any = None
        self._torch: Any = None
        self._transformers: Any = None
        self._eos_ids: tuple[int, ...] = ()
        self._pad_id: int | None = None
        self._context_length: int | None = None
        self._allocated_before_load: float | None = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        """True while the model is loaded and ``generate()`` can be called."""
        return self._model is not None

    def get_model_info(self) -> dict:
        """Model metadata (ruling Q9).

        ``context_length`` is ``None`` until the model is loaded.
        """
        return {
            "model_name": self._model_name,
            "context_length": self._context_length,
            "quantization": QUANTIZATION_LABEL,
            "compute_dtype": COMPUTE_DTYPE,
            "double_quant": DOUBLE_QUANT,
            "chat_template_applied": True,
        }

    # ------------------------------------------------------------------
    # Lifecycle — SDS §5.9 context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> TransformersBackend:
        """Load the tokenizer and the 4-bit model onto CUDA device 0.

        Raises:
            LLMError: When the ``llm`` extra is missing, CUDA is unavailable,
                      VRAM is insufficient (§5.9's message), or loading fails
                      for any other reason. Load failures are fatal for the
                      generation stage (``LLMError`` docstring).
        """
        try:
            torch, transformers = _load_runtime()
        except ImportError as exc:
            raise LLMError(
                f"The LLM runtime is not installed ({exc}). Install the `llm` extra in a "
                f"CUDA environment (pyproject.toml) to use the transformers backend."
            ) from exc

        if not torch.cuda.is_available():
            raise LLMError(
                f"CUDA is not available, so {self._model_name} cannot be loaded. "
                f"The LLM backend is GPU-only (SDS AD-06); there is no CPU fallback."
            )

        self._torch = torch
        self._transformers = transformers
        self._allocated_before_load = self._log_memory("llm_loading")
        start = time.monotonic()
        tokenizer = model = None
        try:
            compute_dtype = getattr(torch, COMPUTE_DTYPE)
            tokenizer = transformers.AutoTokenizer.from_pretrained(self._model_name)
            quantization = transformers.BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type=QUANT_TYPE,
                bnb_4bit_compute_dtype=compute_dtype,
                bnb_4bit_use_double_quant=DOUBLE_QUANT,
            )
            model = transformers.AutoModelForCausalLM.from_pretrained(
                self._model_name,
                quantization_config=quantization,
                dtype=compute_dtype,
                device_map=dict(MODEL_DEVICE_MAP),
            )
            model.eval()
            dropped = self._neutralize_generation_config(model, tokenizer)
        except torch.cuda.OutOfMemoryError as exc:
            tokenizer = model = None
            traceback.clear_frames(exc.__traceback__)
            self._release()
            self._log_memory("llm_unloaded", before=self._allocated_before_load)
            raise LLMError(OOM_MESSAGE.format(model_name=self._model_name)) from exc
        except Exception as exc:
            tokenizer = model = None
            self._release()
            self._log_memory("llm_unloaded", before=self._allocated_before_load)
            raise LLMError(f"Could not load LLM '{self._model_name}': {exc}") from exc

        self._tokenizer = tokenizer
        self._model = model
        self._context_length = getattr(model.config, "max_position_embeddings", None)
        allocated, reserved = self._memory()
        logger.info(
            "llm_loaded",
            backend_id=self.backend_id,
            model_name=self._model_name,
            load_seconds=round(time.monotonic() - start, 1),
            footprint_mib=round(model.get_memory_footprint() / MIB, 1),
            allocated_mib=allocated,
            reserved_mib=reserved,
            quantization=QUANTIZATION_LABEL,
            compute_dtype=COMPUTE_DTYPE,
            double_quant=DOUBLE_QUANT,
            eos_token_ids=list(self._eos_ids),
            dropped_generation_defaults=dropped,
        )
        return self

    def __exit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        """Unload the model and empty the CUDA cache (§5.9, AD-05).

        Runs on every exit path, including when the ``with`` body raised, and
        returns ``None`` so the body's exception is never swallowed. Logs the
        allocated and reserved memory after unloading, next to the value
        logged before loading (ruling A20); a residue is logged, not raised
        (ruling Q10).
        """
        try:
            self._release()
        finally:
            self._log_memory("llm_unloaded", before=self._allocated_before_load)

    def _release(self) -> None:
        """Drop every model reference, collect, then empty the CUDA cache.

        The order matters: ``empty_cache()`` can only return blocks that no
        live tensor still holds.
        """
        self._model = None
        self._tokenizer = None
        gc.collect()
        if self._torch is not None:
            self._torch.cuda.empty_cache()

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate(
        self, prompt: str, params: GenerationParams, system: str | None = None
    ) -> GenerationResult:
        """Run one prompt through the loaded model.

        ``system``, when given, is sent as the system message (SDS 5.9.1).

        Raises:
            RuntimeError: If called outside the context manager.
            LLMError: On any inference failure, including CUDA OOM. The model
                      stays loaded; what the caller does next is ruling A18.
        """
        if self._model is None:
            raise RuntimeError(
                "TransformersBackend.generate() called outside its context manager. "
                "Use `with llm_registry.get_backend() as llm:` first (SDS §5.9)."
            )

        start = time.monotonic()
        try:
            torch, transformers = self._torch, self._transformers
            if params.seed is not None:
                transformers.set_seed(params.seed)
            messages = [{"role": "system", "content": system}] if system is not None else []
            messages.append({"role": "user", "content": prompt})
            wrapped = self._tokenizer.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=False,
            )
            encoded = self._tokenizer(wrapped, return_tensors="pt", add_special_tokens=False)
            encoded = encoded.to(self._model.device)
            prompt_tokens = int(encoded["input_ids"].shape[1])
            extra: dict[str, Any] = {}
            if params.stop_sequences:
                extra["tokenizer"] = self._tokenizer
            with torch.inference_mode():
                output = self._model.generate(
                    **encoded, generation_config=self._generation_config(params), **extra
                )
            new_tokens = [int(t) for t in output[0, prompt_tokens:].tolist()]
            text = self._tokenizer.decode(new_tokens, skip_special_tokens=True)
            stop_reason = self._stop_reason(new_tokens, text, params)
        except Exception as exc:
            raise LLMError(f"Generation failed with '{self._model_name}': {exc}") from exc

        return GenerationResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=len(new_tokens),
            latency_ms=int((time.monotonic() - start) * 1000),
            stop_reason=stop_reason,
        )

    def _generation_config(self, params: GenerationParams) -> Any:
        """A fully specified GenerationConfig from ``params`` only (Q3, Q4)."""
        sample = params.temperature > 0
        return self._transformers.GenerationConfig(
            max_new_tokens=params.max_new_tokens,
            do_sample=sample,
            temperature=params.temperature if sample else 1.0,
            top_p=params.top_p if sample else 1.0,
            top_k=TOP_K_DISABLED,
            repetition_penalty=params.repetition_penalty,
            stop_strings=list(params.stop_sequences) or None,
            eos_token_id=list(self._eos_ids),
            pad_token_id=self._pad_id,
        )

    def _stop_reason(self, new_tokens: list[int], text: str, params: GenerationParams) -> str:
        """eos, then stop_sequence, then max_tokens (ruling Q5).

        A stop string counts when it appears anywhere in ``text``: generation
        stops at the token that completes the first occurrence, and that token
        may run past the string's end ("overhang"), so ``endswith`` would miss
        it.
        """
        if new_tokens and new_tokens[-1] in self._eos_ids:
            return "eos"
        if any(stop in text for stop in params.stop_sequences):
            return "stop_sequence"
        if len(new_tokens) >= params.max_new_tokens:
            return "max_tokens"
        raise LLMError(
            f"Generation ended after {len(new_tokens)} of {params.max_new_tokens} tokens "
            f"without an eos token or a stop sequence; the stop reason is unknown."
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _neutralize_generation_config(self, model: Any, tokenizer: Any) -> dict:
        """Replace the checkpoint's generation config with one holding only token ids.

        Returns the sampling values that were dropped, for the load log.
        """
        hub = model.generation_config
        configured = hub.eos_token_id
        ids = configured if isinstance(configured, list | tuple) else [configured]
        eos = {int(i) for i in ids if i is not None}
        if tokenizer.eos_token_id is not None:
            eos.add(int(tokenizer.eos_token_id))
        self._eos_ids = tuple(sorted(eos))
        pad = tokenizer.pad_token_id
        self._pad_id = int(pad) if pad is not None else (self._eos_ids[0] if eos else None)
        dropped = {
            key: getattr(hub, key, None)
            for key in ("do_sample", "temperature", "top_p", "top_k", "repetition_penalty")
            if getattr(hub, key, None) is not None
        }
        model.generation_config = self._transformers.GenerationConfig(
            bos_token_id=getattr(hub, "bos_token_id", None),
            eos_token_id=list(self._eos_ids),
            pad_token_id=self._pad_id,
        )
        return dropped

    def _memory(self) -> tuple[float | None, float | None]:
        """CUDA allocated and reserved memory in MiB, or ``None`` when unreadable."""
        try:
            cuda = self._torch.cuda
            return (
                round(cuda.memory_allocated() / MIB, 1),
                round(cuda.memory_reserved() / MIB, 1),
            )
        except Exception:
            return None, None

    def _log_memory(self, event: str, before: float | None = None) -> float | None:
        """Log allocated and reserved memory under ``event``; return allocated MiB."""
        allocated, reserved = self._memory()
        fields: dict[str, Any] = {"allocated_mib": allocated, "reserved_mib": reserved}
        if event == "llm_unloaded":
            fields["allocated_before_load_mib"] = before
            fields["allocated_delta_mib"] = (
                round(allocated - before, 1)
                if allocated is not None and before is not None
                else None
            )
        logger.info(event, backend_id=self.backend_id, model_name=self._model_name, **fields)
        return allocated
