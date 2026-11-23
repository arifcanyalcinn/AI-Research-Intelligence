"""
Unit tests for the output parser (SDS 5.10, D-014, D-016, ruling A11).

The final test runs clean parser output through the validator, so the parser
and the D-014 stored format cannot drift apart.
"""

from __future__ import annotations

import json

from arip.config import ValidationConfig
from arip.db.models import GeneratedContent, Item
from arip.enums import SegmentRole
from arip.generation.parser import full_text, parse_output, parsed_segments_json
from arip.generation.validator import ContentValidator

URL = "https://arxiv.org/abs/2411.01234"
TEXTS = {
    "HOOK": "A new way to make small language models reason step by step.",
    "PROBLEM": "Small models often skip steps and reach wrong answers on maths problems.",
    "SOLUTION": "The authors train the model to write each step and check it before going on.",
    "IMPACT": "Teams with one GPU can get more reliable answers from a small open model.",
    "TAKEAWAY": "Checking each step matters more than making the model larger.",
    "SOURCE": f"Read the full paper on arXiv: {URL}",
}
CLEAN = "\n".join(f"[{role}] {text}" for role, text in TEXTS.items())


def roles(segments) -> list[str]:
    return [segment.role.value for segment in segments]


def test_clean_output_parses() -> None:
    segments = parse_output(CLEAN)

    assert roles(segments) == list(TEXTS)
    assert [s.text for s in segments] == list(TEXTS.values())


def test_repeated_marker_is_parse_failure() -> None:
    assert parse_output(CLEAN + "\n[HOOK] Another hook that repeats the role.") is None


def test_preamble_is_dropped() -> None:
    segments = parse_output("Sure! Here is the thread:\n\n" + CLEAN)

    assert segments[0].role is SegmentRole.HOOK
    assert segments[0].text == TEXTS["HOOK"]


def test_full_text_has_no_markers() -> None:
    text = full_text(parse_output(CLEAN))

    assert "[" not in text.replace(URL, "")
    assert text == "\n\n".join(TEXTS.values())


def test_out_of_order_stored_in_canonical_order() -> None:
    shuffled = "\n".join(f"[{role}] {TEXTS[role]}" for role in reversed(list(TEXTS)))

    segments = parse_output(shuffled)

    assert roles(segments) == list(TEXTS)
    assert full_text(segments) == "\n\n".join(TEXTS.values())


def test_markers_case_insensitive() -> None:
    lower = "\n".join(f"[{role.lower()}] {text}" for role, text in TEXTS.items())

    assert roles(parse_output(lower)) == list(TEXTS)


def test_decorated_markers_accepted() -> None:
    forms = ["**[{r}]**", "[ {r} ]:", "__[{r}]__", "  [{r}] :", "*[{r}]*"]
    for form in forms:
        decorated = "\n".join(
            form.format(r=role.title()) + " " + text for role, text in TEXTS.items()
        )
        segments = parse_output(decorated)
        assert segments is not None, form
        assert [s.text for s in segments] == list(TEXTS.values()), form


def test_no_markers_gives_none() -> None:
    assert parse_output("A thread without any markers at all.\nSecond line.") is None
    assert parse_output("") is None


def test_marker_inside_a_line_is_text() -> None:
    """A marker that does not start its line is text, on a marker line or a continuation line."""
    raw = CLEAN.replace(
        TEXTS["IMPACT"], TEXTS["IMPACT"] + " See [SOURCE] below.\nIt is cited in the [SOURCE] part."
    )

    segments = parse_output(raw)

    assert roles(segments) == list(TEXTS)
    assert segments[3].text.endswith("See [SOURCE] below.\nIt is cited in the [SOURCE] part.")


def test_missing_role_parses() -> None:
    raw = "\n".join(f"[{role}] {text}" for role, text in TEXTS.items() if role != "IMPACT")

    assert roles(parse_output(raw)) == ["HOOK", "PROBLEM", "SOLUTION", "TAKEAWAY", "SOURCE"]


def test_continuation_lines_blank_lines_and_trailing_text() -> None:
    raw = CLEAN.replace(
        f"[PROBLEM] {TEXTS['PROBLEM']}", "[PROBLEM]\n\nFirst line.\n\nSecond line.\n"
    )
    raw += "\nThanks for reading!"

    segments = parse_output(raw)

    assert segments[1].text == "First line.\n\nSecond line."
    assert segments[5].text == TEXTS["SOURCE"] + "\nThanks for reading!"


def test_empty_marker_gives_empty_segment() -> None:
    raw = CLEAN.replace(f"[TAKEAWAY] {TEXTS['TAKEAWAY']}", "[TAKEAWAY]")

    assert parse_output(raw)[4].text == ""


def test_parser_output_passes_the_validator() -> None:
    segments = parse_output(CLEAN)
    stored = parsed_segments_json(segments)
    row = GeneratedContent(
        id=1, parsed_segments=stored, full_text=full_text(segments), raw_output=CLEAN
    )
    item = Item(id=1, primary_url=URL, abstract="No numbers.", title="T")

    result = ContentValidator(ValidationConfig()).validate(row, item, "eos")

    assert json.loads(stored) == [{"role": role, "text": text} for role, text in TEXTS.items()]
    assert result.passed, result.errors
    assert result.warnings == []


def test_json_keeps_non_ascii() -> None:
    raw = CLEAN.replace("small language models", "küçük dil modelleri")

    assert "küçük" in parsed_segments_json(parse_output(raw))
