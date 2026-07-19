"""
Data transfer objects for ARIP.

These are plain dataclasses (or Pydantic models where validation is needed)
used to pass data between components. They are NOT ORM models — those live
in arip/db/models.py.

Kept in one file at this scale. If the file grows past ~300 lines, split
by domain (source entities, generation entities, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from arip.enums import ReviewDecisionType, SegmentRole, SourceType

# ---------------------------------------------------------------------------
# Source layer
# ---------------------------------------------------------------------------


@dataclass
class RawSourcePayload:
    """Carrier for a single item fetched from a source plugin.

    This is the contract between BaseSource.fetch() and the collection stage.
    The raw_data dict is the untouched API response — normalization happens
    in BaseSource.normalize() after this object is returned.
    """

    source_id: str
    """Identifier of the source that produced this payload (e.g. 'arxiv')."""

    source_type: SourceType
    """Category of content: PAPER, MODEL, SPACE, or REPO."""

    external_id: str
    """Stable identifier in the source system (e.g. ArXiv paper ID '2401.12345')."""

    raw_data: dict
    """Original API response as a Python dict — never modified after creation."""

    fetched_at: datetime
    """UTC timestamp when the fetch completed."""


@dataclass
class NormalizedItem:
    """Result of BaseSource.normalize() — ready to be inserted into items table.

    Fields map directly to columns in the items table. Optional fields are
    None when the source doesn't provide them.
    """

    source_id: str
    source_type: str
    external_id: str
    content_hash: str
    language: str
    title: str
    primary_url: str
    raw_payload: str  # JSON string of the original raw_data

    # Optional metadata (populated when available from source)
    authors: list[str] | None = None
    institutions: list[str] | None = None
    abstract: str | None = None
    additional_urls: list[str] | None = None
    published_date: str | None = None  # ISO date string: "2024-01-15"
    topics: list[str] | None = None
    source_signals: dict | None = None  # stars, downloads, likes, citations


@dataclass
class SourceHealth:
    """Health status reported by a source plugin."""

    source_id: str
    is_healthy: bool
    last_fetch_at: datetime | None = None
    last_error: str | None = None
    items_fetched_last_run: int = 0


# ---------------------------------------------------------------------------
# Generation layer
# ---------------------------------------------------------------------------


@dataclass
class GenerationParams:
    """Hyperparameters for a single LLM generate() call.

    Defaults match the config model defaults and are overridden per-item
    during retry (seed incremented to produce different outputs).
    """

    temperature: float = 0.7
    max_new_tokens: int = 1024
    top_p: float = 0.95
    repetition_penalty: float = 1.1
    seed: int | None = None
    stop_sequences: list[str] = field(default_factory=list)


@dataclass
class GenerationResult:
    """Raw output from a single LLM generate() call.

    stop_reason is required — 'max_tokens' means the output was truncated.
    The validator treats truncation as a warning (not a hard failure), but
    the reviewer should be aware that the content may be cut off.
    """

    text: str
    """Complete raw text returned by the model."""

    prompt_tokens: int
    """Number of tokens in the rendered prompt."""

    completion_tokens: int
    """Number of tokens in the generated output."""

    latency_ms: int
    """Wall-clock time from generate() call to return, in milliseconds."""

    stop_reason: Literal["stop_sequence", "max_tokens", "eos"]
    """Why generation stopped. 'max_tokens' indicates potential truncation."""


@dataclass
class ContentSegment:
    """A single structured segment of generated content."""

    role: SegmentRole
    text: str


# ---------------------------------------------------------------------------
# Validation layer
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of ContentValidator.validate()."""

    passed: bool
    """True only if all hard checks passed. Warnings do not affect this flag."""

    errors: list[str] = field(default_factory=list)
    """Hard validation failures — each prevents publishing."""

    warnings: list[str] = field(default_factory=list)
    """Soft flags (truncation, hallucination risk) — logged but do not fail."""


# ---------------------------------------------------------------------------
# Publishing layer
# ---------------------------------------------------------------------------


@dataclass
class PublishingResult:
    """Result of a single BasePublisher.publish() call."""

    publisher_id: str
    status: Literal["PUBLISHED", "FAILED"]
    external_id: str | None = None
    """Platform-assigned post ID (e.g. Twitter tweet ID of the root tweet)."""

    external_url: str | None = None
    """URL of the published post."""

    error_message: str | None = None
    """Error description when status='FAILED'."""

    published_at: datetime | None = None


# ---------------------------------------------------------------------------
# Review layer
# ---------------------------------------------------------------------------


@dataclass
class ReviewDecision:
    """Decision from a human reviewer, delivered via the decision queue.

    The Telegram polling thread puts these into queue.Queue; the main thread
    reads them at the start of each pipeline run via check_decision_queue().
    """

    item_id: int
    content_id: int
    decision: ReviewDecisionType
    tracking_id: str
    """Platform message ID — used to look up the review_records row."""

    decided_at: datetime
    reviewer_note: str | None = None
