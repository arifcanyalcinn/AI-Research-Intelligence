"""
Abstract base classes (ABCs) for all plugin extension points in ARIP.

These define the contracts that plugin implementations must satisfy:
  - BaseSource      → arip/sources/
  - BaseEmbedder    → arip/backends/embeddings/
  - BaseLLM         → arip/backends/llm/
  - BaseReviewer    → arip/review/
  - BasePublisher   → arip/publishers/

Adding a new plugin means:
  1. Create a class that inherits from the appropriate ABC.
  2. Implement all abstract methods.
  3. Add one import line to the package's __init__.py.
  4. The registry discovers it via BaseClass.__subclasses__().

No concrete implementation belongs in this file.
"""

from __future__ import annotations

import queue
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from arip.entities import (
    GenerationParams,
    GenerationResult,
    NormalizedItem,
    PublishingResult,
    RawSourcePayload,
    SourceHealth,
)
from arip.enums import SourceType

if TYPE_CHECKING:
    # Avoid circular imports — these are only needed for type hints.
    from arip.db.models import GeneratedContent, Item
    from arip.entities import ReviewDecision


# ---------------------------------------------------------------------------
# Source plugin interface
# ---------------------------------------------------------------------------


class BaseSource(ABC):
    """Abstract base for a data source plugin.

    Each source fetches items from one external system and returns them as
    a list of RawSourcePayload objects. Normalization happens in normalize(),
    which is also a method on this class (each source knows its own schema).

    Subclasses MUST define the class attributes source_id and source_type.
    """

    source_id: ClassVar[str]
    """Unique string identifier for this source, e.g. 'arxiv'."""

    source_type: ClassVar[SourceType]
    """Category of items this source returns."""

    def __init__(self, config: object | None) -> None:
        """
        Args:
            config: The source-specific config sub-model from AppSettings.
                    May be None if the source has no config block — handle
                    gracefully by using built-in defaults.
        """
        self._config = config

    @abstractmethod
    def fetch(self) -> list[RawSourcePayload]:
        """Fetch raw items from the source.

        - Retries on transient network failures (via tenacity).
        - Returns an empty list on permanent failure (auth error, etc.).
        - Never raises — failures are logged and the empty list is returned
          so the pipeline continues with remaining sources.

        Returns:
            List of raw payloads; may be empty if fetch failed or source
            returned no new items.
        """

    @abstractmethod
    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:
        """Map a raw payload to the canonical NormalizedItem schema.

        Called once per payload returned by fetch(). Raises on missing
        required fields (title, primary_url) — the collection stage catches
        this and marks the item FAILED with failed_at_stage='NORMALIZATION'.

        Args:
            payload: A single RawSourcePayload from fetch().

        Returns:
            NormalizedItem with all extractable fields populated.

        Raises:
            arip.exceptions.SourceError: If required fields are missing or
                the payload cannot be parsed.
        """

    @classmethod
    @abstractmethod
    def get_config_schema(cls) -> type:
        """Return the Pydantic model class used to validate this source's config.

        This is a classmethod (not an instance method) so the schema can be
        inspected before the source is instantiated. This resolves the
        chicken-and-egg problem where you'd need an instance to get the schema
        but need the schema to validate config before creating the instance.

        Returns:
            A Pydantic BaseModel subclass.
        """

    def health_check(self) -> SourceHealth:
        """Optional health check. Returns a best-effort status object.

        The default implementation always returns is_healthy=True.
        Sources that can make a cheap health request should override this.
        """
        return SourceHealth(source_id=self.source_id, is_healthy=True)


# ---------------------------------------------------------------------------
# Embedding backend interface
# ---------------------------------------------------------------------------


class BaseEmbedder(ABC):
    """Abstract base for an embedding backend.

    Implemented as a context manager to guarantee model unloading on exit,
    even if an exception occurs during the embedding pass.

    Usage:
        with embedder:
            for item in items:
                vector = embedder.embed([item.title + ". " + (item.abstract or "")])

    The embedding model runs on CPU only (enforced by implementations).
    The LLM backend runs on GPU. They are never in memory simultaneously.
    """

    backend_id: ClassVar[str]
    """Unique identifier for this backend, e.g. 'sentence_transformers'."""

    @property
    @abstractmethod
    def embedding_dim(self) -> int:
        """Dimension of the output vectors (e.g. 384 for all-MiniLM-L6-v2)."""

    @abstractmethod
    def __enter__(self) -> BaseEmbedder:
        """Load the embedding model into CPU RAM."""

    @abstractmethod
    def __exit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        """Unload the model and free CPU RAM."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Compute embeddings for a batch of texts.

        Args:
            texts: One or more text strings to embed.

        Returns:
            List of float32 vectors, one per input text.

        Raises:
            arip.exceptions.EmbeddingError: On model failure.
            RuntimeError: If called outside the context manager (model not loaded).
        """


# ---------------------------------------------------------------------------
# LLM backend interface
# ---------------------------------------------------------------------------


class BaseLLM(ABC):
    """Abstract base for an LLM inference backend.

    Implemented as a context manager to guarantee VRAM release on exit.

    Usage:
        with llm_registry.get_backend() as llm:
            for item in items_to_generate:
                result = llm.generate(rendered_prompt, params)
        # Model is unloaded here; VRAM is freed before the next stage.

    The sequential nature of the synchronous design means only one
    `with llm:` block runs at a time — no explicit locking needed.
    """

    backend_id: ClassVar[str]
    """Unique identifier, e.g. 'transformers', 'vllm', 'stub'."""

    @property
    @abstractmethod
    def is_loaded(self) -> bool:
        """True when the model is loaded and generate() can be called."""

    @abstractmethod
    def __enter__(self) -> BaseLLM:
        """Load model weights into GPU VRAM.

        Raises:
            arip.exceptions.LLMError: On load failure or VRAM exhaustion.
        """

    @abstractmethod
    def __exit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        """Unload model weights and free VRAM.

        Must call torch.cuda.empty_cache() after unloading.
        """

    @abstractmethod
    def generate(self, prompt: str, params: GenerationParams) -> GenerationResult:
        """Run inference and return the model's response.

        Args:
            prompt: Fully-rendered prompt string (Jinja2 template already applied).
            params: Generation hyperparameters.

        Returns:
            GenerationResult with text, token counts, latency, and stop_reason.

        Raises:
            arip.exceptions.LLMError: On inference failure.
            RuntimeError: If called outside the context manager (model not loaded).
        """

    @abstractmethod
    def get_model_info(self) -> dict:
        """Return metadata about the loaded model.

        Keys: model_name, context_length, quantization (or None).
        """


# ---------------------------------------------------------------------------
# Reviewer interface
# ---------------------------------------------------------------------------


class BaseReviewer(ABC):
    """Abstract base for a human review backend.

    The reviewer runs in a background thread (start()/stop()) and communicates
    with the main pipeline thread via a thread-safe queue.Queue.

    The Telegram implementation is the only concrete reviewer in v1.
    AutoReviewer (immediate approval) is used when review.enabled = False.
    """

    reviewer_id: ClassVar[str]
    """Unique identifier, e.g. 'telegram', 'auto'."""

    @abstractmethod
    def start(self, decision_queue: queue.Queue[ReviewDecision]) -> None:
        """Start background polling for review decisions.

        Called once at application startup (if review is enabled).
        Implementations that poll a remote service (e.g. Telegram)
        should start a daemon thread here.

        Args:
            decision_queue: Thread-safe queue where ReviewDecision objects
                            are placed when a reviewer makes a decision.
        """

    @abstractmethod
    def stop(self) -> None:
        """Gracefully stop background polling.

        Called during application shutdown. Should join the background thread
        with a reasonable timeout.
        """

    @abstractmethod
    def submit(self, item: Item, content: GeneratedContent) -> str:
        """Submit an item for review and return the tracking ID.

        Args:
            item: The Item ORM object.
            content: The active GeneratedContent ORM object.

        Returns:
            tracking_id: Platform-specific message identifier used to look up
                         the review_records row when a decision arrives.

        Raises:
            arip.exceptions.ReviewError: If the submission fails.
        """

    @abstractmethod
    def format_preview(self, item: Item, content: GeneratedContent) -> str:
        """Format a human-readable preview of the item and its generated content.

        Used to build the review message shown to the reviewer.
        """


# ---------------------------------------------------------------------------
# Publisher interface
# ---------------------------------------------------------------------------


class BasePublisher(ABC):
    """Abstract base for a publishing backend.

    Publishers receive approved content and post it to an external platform.
    Each publisher is responsible for formatting the content appropriately
    for its platform (character limits, thread structure, etc.).

    CHARACTER_LIMIT is a class constant, not a method — the validator reads
    it before publishing to enforce platform constraints.
    """

    publisher_id: ClassVar[str]
    """Unique identifier, e.g. 'twitter', 'dry_run'."""

    platform: ClassVar[str]
    """Human-readable platform name, e.g. 'Twitter/X', 'Dry Run'."""

    CHARACTER_LIMIT: ClassVar[int]
    """Maximum characters per post segment on this platform."""

    @abstractmethod
    def publish(self, content: GeneratedContent, item: Item) -> PublishingResult:
        """Post content to the platform.

        The publish stage checks for existing PublishingRecord before calling
        this method — implementations do NOT need their own idempotency check.

        Args:
            content: The active GeneratedContent to publish.
            item: The parent Item (for metadata like title, URL).

        Returns:
            PublishingResult with status, external IDs, and error if failed.
        """

    def health_check(self) -> bool:
        """Optional connectivity check run at startup.

        Returns True if the platform is reachable and credentials are valid.
        Default implementation returns True (optimistic).
        """
        return True
