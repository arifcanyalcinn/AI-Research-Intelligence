"""
ContentGenerator — one item's generation: render, generate, parse, validate, store
(SDS 5.10, 5.10.1, D-015).

``generate(item)``:

1. ``last_failure_reason = None``; the template is ``thread`` in the item's
   language (D-013). A ``ConfigError`` (no such template) propagates.
2. The context is ``build_context(item)``; rendering raises
   ``jinja2.UndefinedError`` when a required variable is missing. Then
   ``generate()`` returns ``None`` with ``last_failure_reason`` "Template error:
   …": no LLM call, no row, no attempt used (SDS 5.10.1, ruling A19).
3. Attempts are numbered after the item's highest stored attempt (ruling A6),
   and at most ``max_retries + 1`` are made (SDS 5.10.1).
4. Seeds (ruling A7): ``seed = (base + attempt - 1) mod 2**32``, where the base
   is the configured seed, or, when that is null, one value per call drawn
   with ``secrets``. Not ``random``: ``transformers.set_seed()`` reseeds the
   ``random`` module on every call, so a base drawn from it would follow from
   the previous item's seed. Only the seed differs between attempts.
5. Each attempt sends the rendered body with the template's system text
   (SDS 5.9.1), parses the output (D-016), validates a transient row with the
   attempt's ``stop_reason`` (SDS 5.11.1), and stores the row. A failed
   attempt is stored with ``is_active=False``. A passing one first deactivates
   every earlier row of the item, is stored with ``is_active=True`` and is
   returned: at most one row per item is active, and an active row has passed
   validation.
6. An ``LLMError`` is not retried: ``last_failure_reason`` becomes "LLM error:
   …" and the error propagates; rows of earlier attempts stay in the session
   (SDS 5.10.1).
7. When every attempt fails validation: ``None``, with ``last_failure_reason``
   "Validation failed after N attempts: " and the last attempt's errors.

The generator never commits; the caller owns the session (D-015).
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import TYPE_CHECKING

import jinja2
import structlog

from arip.db.models import GeneratedContent
from arip.entities import GenerationParams as CallParams
from arip.enums import Language
from arip.exceptions import LLMError
from arip.generation.context import build_context
from arip.generation.parser import full_text, parse_output, parsed_segments_json
from arip.generation.prompt_manager import stored_prompt
from arip.generation.validator import stored_messages

if TYPE_CHECKING:
    from arip.config import GenerationParams
    from arip.db.models import Item
    from arip.db.repositories.content import ContentRepository
    from arip.generation.prompt_manager import PromptManager
    from arip.generation.validator import ContentValidator
    from arip.interfaces import BaseLLM

logger = structlog.get_logger(__name__)

TEMPLATE_ID = "thread"
"""The one prompt template of Phase 4 (D-013, ruling Q13)."""

SEED_MODULUS = 2**32
"""Seeds stay in 0 .. 2**32 - 1, the range numpy accepts (SDS 5.15.2)."""


def attempt_seed(base: int, attempt: int) -> int:
    """The seed of a stored attempt number: (base + attempt - 1) mod 2**32 (ruling A7)."""
    return (base + attempt - 1) % SEED_MODULUS


class ContentGenerator:
    """SDS 5.10 with the inputs and failure channel of SDS 5.10.1."""

    def __init__(
        self,
        llm: BaseLLM,
        prompt_manager: PromptManager,
        validator: ContentValidator,
        content_repo: ContentRepository,
        params: GenerationParams,
        max_retries: int,
    ) -> None:
        """
        Args:
            llm: A loaded backend (inside its ``with`` block).
            params: ``config.llm.generation``.
            max_retries: ``config.generation.max_retries``; at most
                         ``max_retries + 1`` attempts per call.
        """
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self._llm = llm
        self._prompt_manager = prompt_manager
        self._validator = validator
        self._content_repo = content_repo
        self._params = params
        self._max_retries = max_retries
        self.last_failure_reason: str | None = None

    def generate(self, item: Item) -> GeneratedContent | None:
        """Generate and store content for ``item`` (SDS 5.10, 5.10.1).

        Returns:
            The active, validated row; or None, with ``last_failure_reason`` set.

        Raises:
            LLMError: From ``llm.generate()``, not retried.
            ConfigError: When no template exists for the item's language.
        """
        self.last_failure_reason = None
        language = Language(item.language)
        template = self._prompt_manager.get_template(TEMPLATE_ID, language)
        try:
            user_prompt = self._prompt_manager.render(TEMPLATE_ID, language, build_context(item))
        except jinja2.UndefinedError as exc:
            self.last_failure_reason = f"Template error: {exc}"
            logger.warning("generation_template_error", item_id=item.id, error=str(exc))
            return None

        full_prompt = stored_prompt(template.system_text, user_prompt)
        model_name = self._llm.get_model_info()["model_name"]
        first = self._content_repo.max_attempt_for_item(item.id) + 1
        base = (
            self._params.seed if self._params.seed is not None else secrets.randbelow(SEED_MODULUS)
        )
        attempts = self._max_retries + 1
        last_errors: list[str] = []

        for offset in range(attempts):
            attempt = first + offset
            seed = attempt_seed(base, attempt)
            params = self._call_params(seed)
            try:
                result = self._llm.generate(user_prompt, params, system=template.system_text)
            except LLMError as exc:
                self.last_failure_reason = f"LLM error: {exc}"
                logger.error(
                    "generation_llm_error", item_id=item.id, attempt=attempt, error=str(exc)
                )
                raise

            segments = parse_output(result.text)
            data = {
                "item_id": item.id,
                "language": item.language,
                "attempt": attempt,
                "is_active": False,
                "prompt_template_id": template.prompt_template_id,
                "full_prompt": full_prompt,
                "raw_output": result.text,
                "parsed_segments": parsed_segments_json(segments) if segments is not None else None,
                "full_text": full_text(segments) if segments is not None else None,
                "llm_backend_id": self._llm.backend_id,
                "model_name": model_name,
                "temperature": params.temperature,
                "max_tokens": params.max_new_tokens,
                "seed": seed,
                "generation_latency_ms": result.latency_ms,
                "generated_at": datetime.utcnow(),
            }
            outcome = self._validator.validate(
                GeneratedContent(**data, validation_passed=False), item, result.stop_reason
            )
            data["validation_passed"] = outcome.passed
            data["validation_errors"] = json.dumps(stored_messages(outcome), ensure_ascii=False)
            logger.info(
                "generation_attempt",
                item_id=item.id,
                attempt=attempt,
                seed=seed,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
                stop_reason=result.stop_reason,
                parsed=segments is not None,
                passed=outcome.passed,
                errors=len(outcome.errors),
                warnings=len(outcome.warnings),
            )
            if outcome.passed:
                self._content_repo.deactivate_all_for_item(item.id)
                return self._content_repo.create({**data, "is_active": True})
            self._content_repo.create(data)
            last_errors = outcome.errors

        self.last_failure_reason = f"Validation failed after {attempts} attempts: " + "; ".join(
            last_errors
        )
        logger.warning(
            "generation_failed", item_id=item.id, attempts=attempts, reason=self.last_failure_reason
        )
        return None

    def _call_params(self, seed: int) -> CallParams:
        """The configured parameters with only the seed replaced."""
        p = self._params
        return CallParams(
            temperature=p.temperature,
            max_new_tokens=p.max_new_tokens,
            top_p=p.top_p,
            repetition_penalty=p.repetition_penalty,
            seed=seed,
            stop_sequences=list(p.stop_sequences),
        )
