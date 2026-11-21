"""
Unit tests for ContentValidator (SDS 5.11, 5.11.1, 5.15.2, D-014).

Transient ORM objects, no database. ``content()`` builds a row in the D-014
stored format: ``parsed_segments`` a JSON list of {role, text} objects in
canonical order, ``full_text`` the texts joined by a blank line.
"""

from __future__ import annotations

import json

import pytest
from structlog.testing import capture_logs

from arip.config import ValidationConfig
from arip.db.models import GeneratedContent, Item
from arip.entities import ValidationResult
from arip.generation.validator import MIN_SEGMENT_CHARS, ContentValidator, stored_messages

URL = "https://arxiv.org/abs/2411.01234"
TEXTS = {
    "HOOK": "A new way to make small language models reason step by step.",
    "PROBLEM": "Small models often skip steps and reach wrong answers on maths problems.",
    "SOLUTION": "The authors train the model to write each step and check it before going on.",
    "IMPACT": "Teams with one GPU can get more reliable answers from a small open model.",
    "TAKEAWAY": "Checking each step matters more than making the model larger.",
    "SOURCE": f"Read the full paper on arXiv: {URL}",
}
ABSTRACT = "We improve accuracy by 12.5% on GSM8K and run 2x faster than the baseline."
TRUNCATION = "Truncation: generation stopped at max_new_tokens; the text may be cut off"


def segments(**overrides: str | None) -> list[tuple[str, str]]:
    """The six default segments; an override of None removes that role."""
    texts = {**TEXTS, **overrides}
    return [(role, text) for role, text in texts.items() if text is not None]


def content(
    pairs: list[tuple[str, str]] | None = None, raw_output: str | None = None
) -> GeneratedContent:
    pairs = segments() if pairs is None else pairs
    full_text = "\n\n".join(text for _, text in pairs)
    return GeneratedContent(
        id=7,
        parsed_segments=json.dumps([{"role": role, "text": text} for role, text in pairs]),
        full_text=full_text,
        raw_output=raw_output
        if raw_output is not None
        else "\n".join(f"[{role}] {text}" for role, text in pairs),
    )


def unparsed() -> GeneratedContent:
    return GeneratedContent(id=8, parsed_segments=None, full_text=None, raw_output="no markers")


def item(url: str | None = URL, abstract: str | None = ABSTRACT, **extra: object) -> Item:
    return Item(id=3, primary_url=url, abstract=abstract, title="A title", **extra)


def validator(**config: object) -> ContentValidator:
    return ContentValidator(ValidationConfig(**config))


def run(row: GeneratedContent, it: Item | None = None, stop_reason: str = "eos", **config: object):
    return validator(**config).validate(row, it if it is not None else item(), stop_reason)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_valid_content_passes() -> None:
    result = run(content())

    assert result == ValidationResult(passed=True, errors=[], warnings=[])


# ---------------------------------------------------------------------------
# 0 Parse
# ---------------------------------------------------------------------------


def test_unparsed_output_fails() -> None:
    result = run(unparsed())

    assert not result.passed
    assert result.errors == ["Parse: the output could not be parsed into the six marked segments"]


def test_malformed_parsed_segments_fails() -> None:
    for raw in (
        "not json",
        json.dumps({"role": "HOOK"}),
        json.dumps([{"role": "INTRO", "text": "x"}]),
    ):
        row = GeneratedContent(id=9, parsed_segments=raw, full_text="x", raw_output="x")
        result = run(row)
        assert result.errors == [
            "Parse: parsed_segments is not a JSON list of {role, text} objects"
        ], raw


def test_truncation_warned_even_when_unparsed() -> None:
    result = run(unparsed(), stop_reason="max_tokens")

    assert result.warnings == [TRUNCATION]


# ---------------------------------------------------------------------------
# 1 Completeness
# ---------------------------------------------------------------------------


def test_completeness_missing_role() -> None:
    result = run(content(segments(IMPACT=None)))

    assert result.errors == ["Completeness: missing segment(s) IMPACT"]


def test_completeness_repeated_role() -> None:
    pairs = segments()
    pairs.insert(1, ("HOOK", TEXTS["HOOK"]))

    result = run(content(pairs))

    assert result.errors == ["Completeness: repeated segment(s) HOOK"]


# ---------------------------------------------------------------------------
# 2 Citation
# ---------------------------------------------------------------------------


def test_url_only_outside_source_fails() -> None:
    pairs = segments(
        HOOK=f"Small models reason better now, see {URL}",
        SOURCE="Read the full paper on arXiv later today.",
    )

    result = run(content(pairs))

    assert result.errors == [f"Citation: the SOURCE segment does not contain {URL}"]


def test_altered_url_fails() -> None:
    cases = [
        ("https://github.com/org/repo/", "Find the code on GitHub: https://github.com/org/repo"),
        (
            "https://arxiv.org/abs/2411.01234",
            "Read the full paper: http://arxiv.org/abs/2411.01234",
        ),
    ]
    for url, source in cases:
        result = run(content(segments(SOURCE=source)), item(url=url))
        assert result.errors == [f"Citation: the SOURCE segment does not contain {url}"], url


def cited(source: str, url: str = URL) -> bool:
    """True when a row whose SOURCE text is ``source`` passes the citation check."""
    errors = run(content(segments(SOURCE=source)), item(url=url)).errors
    return not [e for e in errors if e.startswith("Citation:")]


def test_url_prefix_of_a_longer_url_fails() -> None:
    """Ruling A12 (2026-11-21): .../abs/2411.0123 is not cited by .../abs/2411.01234."""
    short = "https://arxiv.org/abs/2411.0123"

    assert not cited(f"Read the full paper on arXiv: {URL}", url=short)
    assert not cited(f"Read the full paper on arXiv: {URL}.", url=short)


def test_url_followed_by_closing_character_passes() -> None:
    for source in (
        f"Read the full paper on arXiv: {URL}",
        f"Read the full paper on arXiv: {URL}.",
        f"Read the full paper on arXiv: {URL}. It is free.",
        f"Read the full paper on arXiv ({URL}) today.",
        f"Read the full paper on arXiv: {URL}\nIt is free.",
        f"Read the full paper on arXiv: {URL}, it is free.",
        f'Read the full paper on arXiv, "{URL}" today.',
    ):
        assert cited(source), source


def test_url_followed_by_continuing_character_fails() -> None:
    for suffix in (
        "/",
        "5",
        ".5",
        "-v2",
        "_x",
        "~a",
        "?x=1",
        "#s",
        "&a",
        "=1",
        "%20",
        "+1",
        "a",
        ",x",
    ):
        source = f"Read the full paper on arXiv: {URL}{suffix} today."
        assert not cited(source), suffix


def test_later_occurrence_counts() -> None:
    short = "https://arxiv.org/abs/2411.0123"
    source = f"Not {URL} but the paper itself: {short}."

    assert cited(source, url=short)


def test_item_without_primary_url_fails() -> None:
    result = run(content(), item(url=None))

    assert result.errors == ["Citation: the item has no primary_url"]


# ---------------------------------------------------------------------------
# 3 Min length, 4 Max length
# ---------------------------------------------------------------------------


def test_min_length_boundary() -> None:
    assert MIN_SEGMENT_CHARS == 30
    exactly = "x" * 30
    short = "  " + "y" * 29 + "  "

    assert run(content(segments(TAKEAWAY=exactly))).passed
    result = run(content(segments(TAKEAWAY=short)))
    assert result.errors == ["Min length: TAKEAWAY has 29 characters; at least 30 are required"]


def test_max_length_boundary_uses_config() -> None:
    row = content()
    limit = len(row.full_text)

    assert run(row, max_full_text_chars=limit).passed
    result = run(row, max_full_text_chars=limit - 1)
    assert result.errors == [
        f"Max length: the text has {limit} characters; the limit is {limit - 1}"
    ]


def test_default_limit_is_3000() -> None:
    long = "z" * 2990
    row = content(segments(HOOK=long))
    assert len(row.full_text) > 3000

    assert run(row).errors[0].startswith("Max length:")


# ---------------------------------------------------------------------------
# 5 Truncation
# ---------------------------------------------------------------------------


def test_truncation_is_warning_not_error() -> None:
    result = run(content(), stop_reason="max_tokens")

    assert result.passed
    assert result.errors == []
    assert result.warnings == [TRUNCATION]


def test_eos_and_stop_sequence_give_no_warning() -> None:
    for stop_reason in ("eos", "stop_sequence"):
        assert run(content(), stop_reason=stop_reason).warnings == [], stop_reason


def test_unknown_stop_reason_raises() -> None:
    with pytest.raises(ValueError, match="stop_reason"):
        run(content(), stop_reason="length")


# ---------------------------------------------------------------------------
# 6 Prohibited phrases
# ---------------------------------------------------------------------------


def test_blocklisted_phrase_fails() -> None:
    result = run(content(), prohibited_phrases=["small open model"])

    assert result.errors == ["Prohibited phrase: 'small open model'"]


def test_blocklist_case_insensitive() -> None:
    result = run(content(), prohibited_phrases=["SMALL Open MODEL"])

    assert result.errors == ["Prohibited phrase: 'SMALL Open MODEL'"]


def test_blocklist_checks_full_text_only() -> None:
    row = content(raw_output="Sure, here is the thread:\n[HOOK] ...")

    assert run(row, prohibited_phrases=["sure, here is"]).passed


def test_empty_blocklist_passes() -> None:
    assert run(content(), prohibited_phrases=[]).passed


# ---------------------------------------------------------------------------
# 7 Numbers (hallucination risk)
# ---------------------------------------------------------------------------


def test_unsupported_number_is_warning() -> None:
    row = content(segments(IMPACT="It answers 42% more questions correctly on a single GPU."))

    result = run(row)

    assert result.passed
    assert result.warnings == [
        "Unverified number: '42%' does not appear in the abstract or the source signals"
    ]


def test_supported_numbers_give_no_warning() -> None:
    row = content(
        segments(IMPACT="Accuracy rises by 12.5 % and the model runs 2x faster on one GPU.")
    )

    assert run(row).warnings == []


def test_number_boundary() -> None:
    covered_by_longer = content(
        segments(IMPACT="Accuracy rises by 5% and runs 12x faster on one GPU.")
    )
    it = item(abstract="Accuracy rises by 15% and it runs 12.5x faster.")

    result = run(covered_by_longer, it)

    assert result.warnings == [
        "Unverified number: '5%' does not appear in the abstract or the source signals",
        "Unverified number: '12x' does not appear in the abstract or the source signals",
    ]


def test_number_in_title_only_still_warns() -> None:
    row = content(segments(IMPACT="It cuts the error rate by 30% on the standard benchmark."))
    it = Item(id=3, primary_url=URL, abstract="No figures here.", title="Cutting errors by 30%")

    assert run(row, it).warnings == [
        "Unverified number: '30%' does not appear in the abstract or the source signals"
    ]


def test_number_in_signals_is_supported() -> None:
    row = content(segments(IMPACT="The repository grew 42x in a single week of activity."))
    it = item(abstract="A tool.", source_signals=json.dumps({"stars": 42}))

    assert run(row, it).warnings == []


# ---------------------------------------------------------------------------
# Results, storage and logging
# ---------------------------------------------------------------------------


def test_warnings_do_not_fail() -> None:
    row = content(segments(IMPACT="It answers 42% more questions correctly on a single GPU."))

    result = run(row, stop_reason="max_tokens")

    assert result.passed
    assert len(result.warnings) == 2


def test_all_errors_collected() -> None:
    row = content(segments(IMPACT=None, HOOK="Too short."))

    result = run(row, prohibited_phrases=["making the model larger"])

    assert result.errors == [
        "Completeness: missing segment(s) IMPACT",
        "Min length: HOOK has 10 characters; at least 30 are required",
        "Prohibited phrase: 'making the model larger'",
    ]


def test_stored_messages_prefix_warnings() -> None:
    result = ValidationResult(passed=False, errors=["e1", "e2"], warnings=["w1"])

    assert stored_messages(result) == ["e1", "e2", "WARNING: w1"]


def test_warnings_logged_at_warning_level() -> None:
    with capture_logs() as logs:
        run(content(), stop_reason="max_tokens")

    warnings = [e for e in logs if e["event"] == "validation_warning"]
    assert [(e["log_level"], e["warning"], e["item_id"], e["content_id"]) for e in warnings] == [
        ("warning", TRUNCATION, 3, 7)
    ]
