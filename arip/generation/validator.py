"""
ContentValidator — the quality and format checks of SDS 5.11 (5.11.1, 5.15.2, D-014).

``validate(content, item, stop_reason)`` runs every check and collects every
error (ruling Q12); ``passed`` is true when there is no error. Warnings never
fail a row and never cause a retry.

| # | Check | Rule | Result |
|---|---|---|---|
| 0 | Parse | ``parsed_segments`` is ``None`` or not the D-014 JSON | error; 1-4, 6, 7 skipped |
| 1 | Completeness | each of the six roles exactly once | error |
| 2 | Citation | ``item.primary_url`` verbatim in the SOURCE text, at a URL boundary | error |
| 3 | Min length | each segment, stripped, at least 30 characters | error |
| 4 | Max length | ``len(full_text) <= max_full_text_chars`` | error |
| 5 | Truncation | ``stop_reason == "max_tokens"`` (also when unparsed) | warning |
| 6 | Prohibited phrases | casefolded substring of ``full_text`` | error |
| 7 | Numbers | a % or "N x" claim absent from abstract and signals | warning |

**Stored format (D-014, ruling Q11).** ``parsed_segments`` is a JSON list of
``{"role": <SegmentRole value>, "text": <str>}`` objects in canonical order;
``full_text`` is the segment texts joined by a blank line, without markers. The
parser that writes them is Stage 7.

**Citation boundary (D-014, ruling A12, 2026-11-21).** The exact
``primary_url`` must occur in a SOURCE segment with, right after it, the end of
the text, whitespace, or a closing character that cannot continue a URL
(``)``, ``]``, ``}``, ``>``, ``"``, ``'``). A ``.`` or ``,`` closes it only when
followed by the end of the text or whitespace. Anything else continues the URL
and fails: a letter, a digit, ``/``, ``-``, ``_``, ``~``, ``?``, ``#``, ``&``,
``=``, ``%``, ``+``, a ``.`` followed by a non-space, and every other character.
Every occurrence is tried. No normalisation.

**Stored warnings (SDS 5.11.1).** ``stored_messages()`` gives the
``validation_errors`` list: every error as written, then every warning
prefixed ``WARNING: ``. Each warning is also logged at WARNING level.

Only ``max_full_text_chars`` and ``prohibited_phrases`` are configurable
(``ValidationConfig``, SDS 5.15.2); everything else here is fixed.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

import structlog

from arip.entities import ValidationResult
from arip.enums import SegmentRole

if TYPE_CHECKING:
    from arip.config import ValidationConfig
    from arip.db.models import GeneratedContent, Item

logger = structlog.get_logger(__name__)

STOP_REASONS = frozenset({"eos", "stop_sequence", "max_tokens"})
"""The three values SDS 5.9 defines."""

MIN_SEGMENT_CHARS = 30
"""SDS 5.11 "Min length"; fixed (SDS 5.15.2)."""

WARNING_PREFIX = "WARNING: "
"""Prefix of a warning in the stored validation_errors list (SDS 5.11.1)."""

REQUIRED_ROLES: tuple[str, ...] = tuple(role.value for role in SegmentRole)

URL_CLOSERS = frozenset(")]}>\"'")
"""Characters that end a cited URL wherever they stand (ruling A12)."""

SENTENCE_STOPS = frozenset(".,")
"""End a cited URL only when followed by the end of the text or whitespace."""

PERCENT_CLAIM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s?%")
TIMES_CLAIM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s?[x×](?![A-Za-z0-9])")
"""SDS 5.11's two example patterns (ruling A14 (i)): percentages and "N x"."""


def stored_messages(result: ValidationResult) -> list[str]:
    """The ``generated_content.validation_errors`` list (SDS 5.11.1)."""
    return list(result.errors) + [WARNING_PREFIX + warning for warning in result.warnings]


def _number_present(number: str, source: str) -> bool:
    """``number`` occurs in ``source`` and is not part of a longer number.

    "5" is not covered by "15", and "12" is not covered by "12.5".
    """
    pattern = rf"(?<![\d.]){re.escape(number)}(?!\d|\.\d)"
    return re.search(pattern, source) is not None


def _url_boundary(text: str, index: int) -> bool:
    """True when ``text[index:]`` cannot continue a URL that ends at ``index``."""
    if index == len(text):
        return True
    char = text[index]
    if char.isspace() or char in URL_CLOSERS:
        return True
    if char in SENTENCE_STOPS:
        return index + 1 == len(text) or text[index + 1].isspace()
    return False


def _url_cited(url: str, text: str) -> bool:
    """``url`` occurs in ``text`` at least once followed by a URL boundary (ruling A12)."""
    start = text.find(url)
    while start != -1:
        if _url_boundary(text, start + len(url)):
            return True
        start = text.find(url, start + 1)
    return False


def _load_segments(raw: str) -> list[tuple[str, str]] | None:
    """The D-014 JSON as (role, text) pairs, or None when it is not that shape."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    segments: list[tuple[str, str]] = []
    for entry in data:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"role", "text"}
            or entry["role"] not in REQUIRED_ROLES
            or not isinstance(entry["text"], str)
        ):
            return None
        segments.append((entry["role"], entry["text"]))
    return segments


def _signal_values(raw: str | None) -> list[str]:
    if raw is None:
        return []
    try:
        data: Any = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(value) for value in data.values()] if isinstance(data, dict) else []


class ContentValidator:
    """SDS 5.11 checks, configured by ``ValidationConfig`` (SDS 5.15.2)."""

    def __init__(self, config: ValidationConfig) -> None:
        self._config = config

    def validate(self, content: GeneratedContent, item: Item, stop_reason: str) -> ValidationResult:
        """Run every check on one generated row (SDS 5.11, 5.11.1).

        Raises:
            ValueError: If ``stop_reason`` is not one of the three SDS 5.9 values.
        """
        if stop_reason not in STOP_REASONS:
            raise ValueError(
                f"stop_reason must be one of {sorted(STOP_REASONS)}, got {stop_reason!r}"
            )
        errors: list[str] = []
        warnings: list[str] = []

        segments: list[tuple[str, str]] | None = None
        if content.parsed_segments is None:
            errors.append("Parse: the output could not be parsed into the six marked segments")
        else:
            segments = _load_segments(content.parsed_segments)
            if segments is None:
                errors.append("Parse: parsed_segments is not a JSON list of {role, text} objects")

        if segments is not None:
            errors.extend(self._structure_errors(segments, item, content.full_text))

        if stop_reason == "max_tokens":
            warnings.append(
                "Truncation: generation stopped at max_new_tokens; the text may be cut off"
            )

        if segments is not None and content.full_text is not None:
            errors.extend(self._phrase_errors(content.full_text))
            warnings.extend(self._number_warnings(content.full_text, item))

        for warning in warnings:
            logger.warning(
                "validation_warning", item_id=item.id, content_id=content.id, warning=warning
            )
        return ValidationResult(passed=not errors, errors=errors, warnings=warnings)

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def _structure_errors(
        self, segments: list[tuple[str, str]], item: Item, full_text: str | None
    ) -> list[str]:
        errors: list[str] = []
        counts = Counter(role for role, _ in segments)
        missing = [role for role in REQUIRED_ROLES if counts[role] == 0]
        repeated = [role for role in REQUIRED_ROLES if counts[role] > 1]
        if missing:
            errors.append(f"Completeness: missing segment(s) {', '.join(missing)}")
        if repeated:
            errors.append(f"Completeness: repeated segment(s) {', '.join(repeated)}")

        sources = [text for role, text in segments if role == SegmentRole.SOURCE.value]
        url = item.primary_url
        if url is None or not url.strip():
            errors.append("Citation: the item has no primary_url")
        elif sources and not any(_url_cited(url, text) for text in sources):
            errors.append(f"Citation: the SOURCE segment does not contain {url}")

        for role, text in segments:
            length = len(text.strip())
            if length < MIN_SEGMENT_CHARS:
                errors.append(
                    f"Min length: {role} has {length} characters; "
                    f"at least {MIN_SEGMENT_CHARS} are required"
                )

        limit = self._config.max_full_text_chars
        if full_text is None:
            errors.append("Max length: full_text is missing")
        elif len(full_text) > limit:
            errors.append(
                f"Max length: the text has {len(full_text)} characters; the limit is {limit}"
            )
        return errors

    def _phrase_errors(self, full_text: str) -> list[str]:
        folded = full_text.casefold()
        return [
            f"Prohibited phrase: '{phrase}'"
            for phrase in self._config.prohibited_phrases
            if phrase.casefold() in folded
        ]

    def _number_warnings(self, full_text: str, item: Item) -> list[str]:
        source = "\n".join([item.abstract or "", *_signal_values(item.source_signals)])
        warnings: list[str] = []
        for pattern in (PERCENT_CLAIM, TIMES_CLAIM):
            for match in pattern.finditer(full_text):
                claim, number = match.group(0), match.group(1)
                if not _number_present(number, source):
                    warning = (
                        f"Unverified number: '{claim}' does not appear in the abstract "
                        f"or the source signals"
                    )
                    if warning not in warnings:
                        warnings.append(warning)
        return warnings
