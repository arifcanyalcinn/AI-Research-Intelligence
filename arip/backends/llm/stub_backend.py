"""
StubLLM — an LLM backend that loads no model (SDS §5.9 Testing Strategy).

§5.9: "Implement a `StubLLM` backend that returns configurable canned responses
without loading any model. All pipeline tests use `StubLLM`." The Phase 4 gate
requires "`StubLLM` tests pass independently of any GPU".

Behaviour (ruling Q15):

* canned responses, as strings or complete ``GenerationResult`` objects, are
  returned in order, and the last one is repeated once the list runs out;
* the ``stop_reason`` of string responses is configurable;
* a configured ``LLMError`` can be raised on chosen call numbers (1-based),
  for the A18 failure paths in Stages 7 and 8;
* every prompt and its params are recorded in ``calls``, including a call
  that raises, and the ``system`` text of each call in ``systems``
  (SDS 5.9.1);
* the lifecycle matches the real backend: ``generate()`` outside the context
  manager raises ``RuntimeError``, and ``is_loaded`` is false after exit.

It imports neither torch nor transformers, so it works on a ``[dev]``-only
install. ``get_model_info()`` reports the model as ``"stub:<model>"``, the
marking ``StubEmbedder`` uses, so stub output is never labelled with a real
model's name.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import ClassVar

from arip.entities import GenerationParams, GenerationResult
from arip.exceptions import LLMError
from arip.interfaces import BaseLLM

STUB_MODEL_NAME_PREFIX = "stub:"
DEFAULT_STUB_RESPONSE = "Stub response."


class StubLLM(BaseLLM):
    """Canned responses, no model. Direct subclass of ``BaseLLM`` per D-004."""

    backend_id: ClassVar[str] = "stub"

    def __init__(
        self,
        model_name: str = "stub-model",
        responses: Sequence[str | GenerationResult] | None = None,
        stop_reason: str = "eos",
        fail_on_calls: Iterable[int] = (),
        error: LLMError | None = None,
    ) -> None:
        """
        Args:
            model_name: The configured model name; reported as ``stub:<name>``.
            responses: Returned in order; the last is repeated. Default: one
                       fixed string.
            stop_reason: ``stop_reason`` of string responses.
            fail_on_calls: 1-based call numbers on which ``generate()`` raises.
            error: The ``LLMError`` raised on those calls. Default: a generic one.
        """
        self._model_name = model_name
        self._responses: list[str | GenerationResult] = list(responses or [DEFAULT_STUB_RESPONSE])
        self._stop_reason = stop_reason
        self._fail_on_calls = frozenset(fail_on_calls)
        self._error = error
        self._loaded = False
        self.calls: list[tuple[str, GenerationParams]] = []
        self.systems: list[str | None] = []

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def __enter__(self) -> StubLLM:
        self._loaded = True
        return self

    def __exit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        """Mark the stub unloaded. Nothing is on the GPU, so nothing to empty."""
        self._loaded = False

    def generate(
        self, prompt: str, params: GenerationParams, system: str | None = None
    ) -> GenerationResult:
        if not self._loaded:
            raise RuntimeError(
                "StubLLM.generate() called outside its context manager. "
                "Use `with llm_registry.get_backend() as llm:` first (SDS §5.9)."
            )
        self.calls.append((prompt, params))
        self.systems.append(system)
        number = len(self.calls)
        if number in self._fail_on_calls:
            raise self._error or LLMError(f"StubLLM: configured failure on call {number}")
        response = self._responses[min(number, len(self._responses)) - 1]
        if isinstance(response, GenerationResult):
            return response
        return GenerationResult(
            text=response,
            prompt_tokens=len(prompt.split()),
            completion_tokens=len(response.split()),
            latency_ms=0,
            stop_reason=self._stop_reason,  # type: ignore[arg-type]
        )

    def get_model_info(self) -> dict:
        return {
            "model_name": STUB_MODEL_NAME_PREFIX + self._model_name,
            "context_length": None,
            "quantization": None,
        }
