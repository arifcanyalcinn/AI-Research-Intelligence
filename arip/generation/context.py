"""
Context builder — maps an item to the prompt template's variables (SDS 5.10, D-016, ruling A10).

``build_context(item)`` returns exactly the variables ``thread_en.j2`` declares
(D-013): required ``title``, ``primary_url``, ``source_type``, ``source_id``;
optional ``abstract``, ``authors``, ``published_date``, ``source_signals``. No
ranking field is passed. The mapping is the one the Stage 6 measurement used,
so the measured prompt sizes apply:

| Variable | From |
|---|---|
| ``title``, ``primary_url``, ``source_type``, ``source_id`` | the column, unchanged |
| ``abstract`` | ``abstract.strip()`` |
| ``authors`` | the JSON list, entries as stripped strings, blanks dropped |
| ``published_date`` | ``published_date.isoformat()`` |
| ``source_signals`` | the JSON object, unchanged |

JSON that does not parse, or is not a list (authors) or an object (signals),
counts as missing. Missing values are passed as ``None``; the renderer drops
them before rendering (ruling Q6, D-013), so the builder does not decide what
"missing" means.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arip.db.models import Item

CONTEXT_KEYS: tuple[str, ...] = (
    "title",
    "primary_url",
    "source_type",
    "source_id",
    "abstract",
    "authors",
    "published_date",
    "source_signals",
)


def _json(raw: str | None, kind: type) -> Any:
    """The parsed JSON value when it is of ``kind``, else None."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, kind) else None


def build_context(item: Item) -> dict[str, Any]:
    """The template context for ``item`` (keys: ``CONTEXT_KEYS``)."""
    authors = _json(item.authors, list)
    if authors is not None:
        authors = [str(name).strip() for name in authors if str(name).strip()]
    return {
        "title": item.title,
        "primary_url": item.primary_url,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "abstract": item.abstract.strip() if item.abstract is not None else None,
        "authors": authors,
        "published_date": (
            item.published_date.isoformat() if item.published_date is not None else None
        ),
        "source_signals": _json(item.source_signals, dict),
    }
