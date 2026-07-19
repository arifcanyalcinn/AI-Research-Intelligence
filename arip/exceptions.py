"""
Exception hierarchy for ARIP.

Seven exception types total (plus InvalidTransitionError).
Sub-classes exist only where the caller would handle them differently.

    ARIPError (base)
    ├── ConfigError
    ├── SourceError
    ├── LLMError
    ├── EmbeddingError
    ├── ContentValidationError      (named to avoid collision with pydantic.ValidationError)
    ├── ReviewError
    └── PublisherError

InvalidTransitionError is a sub-class of ARIPError because transition violations
are a programming error, not a runtime failure — they indicate a bug in the
pipeline stage code and should never be caught and swallowed.
"""


class ARIPError(Exception):
    """Base exception for all ARIP errors."""


class ConfigError(ARIPError):
    """Invalid or missing application configuration.

    Raised at startup when AppSettings validation fails, a required field is
    missing, or signal weights don't sum to 1.0. The process should exit after
    logging this — there is no sensible way to continue with bad config.
    """


class SourceError(ARIPError):
    """Failure fetching from an external data source.

    Covers network timeouts, authentication failures, rate limits, and
    malformed API responses. All are handled the same way: log the error,
    return an empty list from fetch(), and let the pipeline continue with
    the remaining sources.
    """


class LLMError(ARIPError):
    """LLM backend failure.

    Covers model load failures, VRAM exhaustion (torch.cuda.OutOfMemoryError
    is caught and re-raised as LLMError), and generation errors. Generation
    errors trigger a per-item retry (different seed); model load failures are
    fatal for the entire generation stage.
    """


class EmbeddingError(ARIPError):
    """Embedding backend failure.

    Covers model download failure on first run, OOM on CPU RAM (extremely
    unlikely with all-MiniLM-L6-v2), and ANN index corruption. ANN index
    corruption should trigger a rebuild from scratch.
    """


class ContentValidationError(ARIPError):
    """Generated content failed quality validation.

    Named ContentValidationError (not ValidationError) to avoid collision
    with pydantic.ValidationError. Raised by ContentValidator when content
    fails a hard check (missing segments, missing source citation, etc.).
    Warnings (truncation, hallucination flags) do not raise — they are
    recorded in validation_errors and logged.
    """


class ReviewError(ARIPError):
    """Failure in the human review subsystem.

    Covers Telegram API errors during submission, failure to start the
    polling thread, and malformed callback data received from the bot.
    """


class PublisherError(ARIPError):
    """Failure publishing to an external platform.

    Covers network errors, API rate limits, and authentication failures
    during publishing. The publish stage catches this and records the
    failure in publishing_records for retry on the next pipeline run.
    """


class InvalidTransitionError(ARIPError):
    """Attempt to make a state transition not in the valid transition matrix.

    This exception indicates a bug in the pipeline stage code — it called
    StateMachine.transition() with a (from, to) pair that is not in the
    frozenset of valid transitions.

    It is intentionally NOT caught anywhere in the pipeline. Let it propagate
    to the top level so the bug is visible immediately.
    """
