"""
Unit tests for build_context (SDS 5.10, D-016, ruling A10).

Transient Item objects, no database. The keys are checked against the shipped
thread_en.j2, so the builder and the template cannot drift apart.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from arip.db.models import Item
from arip.enums import Language
from arip.generation.context import CONTEXT_KEYS, build_context
from arip.generation.prompt_manager import PromptManager

REPO = Path(__file__).resolve().parents[3]


def item(**overrides: object) -> Item:
    values: dict[str, object] = {
        "id": 1,
        "title": "Attention Is All You Need",
        "primary_url": "https://arxiv.org/abs/1706.03762",
        "source_type": "PAPER",
        "source_id": "arxiv",
        "abstract": "  We propose the Transformer.\n",
        "authors": json.dumps(["Ashish Vaswani", "Noam Shazeer"]),
        "published_date": date(2017, 6, 12),
        "source_signals": json.dumps({"upvotes": 42}),
        "importance_score": 0.9,
        "signal_breakdown": json.dumps({"recency": 1.0}),
        "language": "EN",
    }
    values.update(overrides)
    return Item(**values)


def test_context_strips_abstract() -> None:
    assert build_context(item())["abstract"] == "We propose the Transformer."
    assert build_context(item(abstract=None))["abstract"] is None


def test_context_treats_unusable_json_as_missing() -> None:
    for authors in ("not json", json.dumps({"a": 1}), json.dumps("x"), None):
        assert build_context(item(authors=authors))["authors"] is None, authors
    for signals in ("not json", json.dumps([1, 2]), json.dumps(3), None):
        assert build_context(item(source_signals=signals))["source_signals"] is None, signals


def test_context_published_date_is_iso() -> None:
    assert build_context(item())["published_date"] == "2017-06-12"
    assert build_context(item(published_date=None))["published_date"] is None


def test_context_cleans_authors() -> None:
    context = build_context(item(authors=json.dumps([" Ada ", "", "  ", 3])))

    assert context["authors"] == ["Ada", "3"]


def test_context_passes_columns_and_signals_unchanged() -> None:
    context = build_context(item())

    assert context["title"] == "Attention Is All You Need"
    assert context["primary_url"] == "https://arxiv.org/abs/1706.03762"
    assert context["source_type"] == "PAPER"
    assert context["source_id"] == "arxiv"
    assert context["source_signals"] == {"upvotes": 42}


def test_context_keys_are_the_shipped_template_variables() -> None:
    template = PromptManager(REPO / "config" / "prompts").get_template("thread", Language.EN)
    declared = set(template.required_variables) | set(template.optional_variables)

    assert set(build_context(item())) == declared == set(CONTEXT_KEYS)
    assert "importance_score" not in build_context(item())


def test_context_renders_with_the_shipped_template() -> None:
    text = PromptManager(REPO / "config" / "prompts").render(
        "thread", Language.EN, build_context(item(authors=None, source_signals="bad"))
    )

    assert "Description:\nWe propose the Transformer." in text
    assert "Authors:" not in text
    assert "Signals:" not in text
