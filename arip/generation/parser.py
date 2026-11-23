"""
Output parser — splits the model's text on the six segment markers (SDS 5.10, D-016, ruling A11).

``thread_en.j2`` asks for: "Begin each part on a new line with its marker in
square brackets, followed by the text on the same line". A marker is a line
that starts (after optional whitespace) with ``[HOOK]``, ``[PROBLEM]``,
``[SOLUTION]``, ``[IMPACT]``, ``[TAKEAWAY]`` or ``[SOURCE]``. Tolerated:

* any letter case (``[Hook]``);
* up to two ``*`` or ``_`` around the marker (``**[HOOK]**``), spaces inside the
  brackets, and a ``:`` after it;
* text before the first marker (a preamble): dropped, its length logged;
* blank lines inside a segment: kept; the segment is stripped at both ends;
* segments out of order: accepted and stored in canonical order;
* a marker with no text: an empty segment (the 30-character check fails it);
* a missing role: parses (the validator's completeness check fails it).

A marker in the middle of a line is ordinary text. Text after SOURCE belongs to
SOURCE. Parse failure, ``None``: no marker at all, or a role that appears twice.

Stored format (D-014): ``parsed_segments_json()`` gives a JSON list of
``{"role", "text"}`` objects in canonical order (``ensure_ascii=False``);
``full_text()`` gives the texts joined by a blank line, without markers.
"""

from __future__ import annotations

import json
import re

import structlog

from arip.entities import ContentSegment
from arip.enums import SegmentRole

logger = structlog.get_logger(__name__)

CANONICAL_ORDER: tuple[SegmentRole, ...] = tuple(SegmentRole)
SEGMENT_JOIN = "\n\n"

MARKER = re.compile(
    r"^\s*[*_]{0,2}\s*\[\s*(HOOK|PROBLEM|SOLUTION|IMPACT|TAKEAWAY|SOURCE)\s*\]\s*[*_]{0,2}\s*:?\s*(.*)$",
    re.IGNORECASE,
)


def parse_output(raw: str) -> list[ContentSegment] | None:
    """The segments of ``raw`` in canonical order, or None when it cannot be parsed."""
    found: dict[SegmentRole, list[str]] = {}
    current: SegmentRole | None = None
    preamble: list[str] = []
    for line in raw.splitlines():
        match = MARKER.match(line)
        if match:
            role = SegmentRole(match.group(1).upper())
            if role in found:
                logger.info("parse_repeated_marker", role=role.value)
                return None
            found[role] = [match.group(2)]
            current = role
        elif current is None:
            preamble.append(line)
        else:
            found[current].append(line)
    if not found:
        logger.info("parse_no_markers", raw_chars=len(raw))
        return None
    dropped = "\n".join(preamble).strip()
    if dropped:
        logger.info("parse_preamble_dropped", chars=len(dropped))
    return [
        ContentSegment(role=role, text="\n".join(found[role]).strip())
        for role in CANONICAL_ORDER
        if role in found
    ]


def parsed_segments_json(segments: list[ContentSegment]) -> str:
    """The ``generated_content.parsed_segments`` value (D-014)."""
    return json.dumps(
        [{"role": segment.role.value, "text": segment.text} for segment in segments],
        ensure_ascii=False,
    )


def full_text(segments: list[ContentSegment]) -> str:
    """The ``generated_content.full_text`` value (D-014): texts joined by a blank line."""
    return SEGMENT_JOIN.join(segment.text for segment in segments)
