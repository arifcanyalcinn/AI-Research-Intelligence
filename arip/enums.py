"""
Enumerations for ARIP.

All string enums so their values are human-readable in the database
and in log output.
"""

from enum import Enum


class ItemStatus(str, Enum):
    """Lifecycle states for a discovered item.

    Twelve states in total: 8 active (item is being processed) and
    4 terminal (processing is complete — either successfully or not).

    Terminal states ARCHIVED, DUPLICATE, and FILTERED are truly terminal.
    FAILED is terminal by default but can be manually re-queued via CLI.
    """

    # Active states — item is progressing through the pipeline
    COLLECTED = "COLLECTED"
    RANKED = "RANKED"
    EMBEDDED = "EMBEDDED"
    ENRICHED = "ENRICHED"
    GENERATED = "GENERATED"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"

    # Terminal states — no further automatic transitions
    ARCHIVED = "ARCHIVED"
    DUPLICATE = "DUPLICATE"
    FILTERED = "FILTERED"
    FAILED = "FAILED"


class Language(str, Enum):
    """Supported output languages for generated content."""

    EN = "EN"
    TR = "TR"


class SourceType(str, Enum):
    """Category of item returned by a source plugin."""

    PAPER = "PAPER"
    MODEL = "MODEL"
    SPACE = "SPACE"
    REPO = "REPO"


class SegmentRole(str, Enum):
    """Structural roles in a generated content thread.

    The LLM is instructed to produce exactly one segment per role.
    The validator enforces that all six are present.
    """

    HOOK = "HOOK"
    PROBLEM = "PROBLEM"
    SOLUTION = "SOLUTION"
    IMPACT = "IMPACT"
    TAKEAWAY = "TAKEAWAY"
    SOURCE = "SOURCE"


class PublisherStatus(str, Enum):
    """Status of a single publishing attempt recorded in publishing_records."""

    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class PipelineRunStatus(str, Enum):
    """Status of a pipeline run recorded in pipeline_runs."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReviewDecisionType(str, Enum):
    """Possible decisions a human reviewer can make."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REGENERATE = "REGENERATE"
