"""
SQLAlchemy ORM models for ARIP.

Six tables, matching the schema in SDS Section 4 exactly.
All relationships are defined here; queries belong in repositories.

Column naming mirrors the SDS schema tables column-for-column.
JSON columns are stored as TEXT in SQLite and serialized/deserialized
by the application layer (not by SQLAlchemy type decorators) to keep
the ORM layer simple and avoid hidden magic.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all ARIP ORM models."""


# ---------------------------------------------------------------------------
# items — the central table
# ---------------------------------------------------------------------------


class Item(Base):
    """One discovered item at any lifecycle stage.

    This is the central table. Every other table references it.
    Status transitions are managed exclusively by StateMachine — never
    by direct assignment to item.status outside of StateMachine.transition().
    """

    __tablename__ = "items"

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_items_source_external"),
        UniqueConstraint("content_hash", name="uq_items_content_hash"),
        Index("ix_items_status", "status"),
        Index("ix_items_collected_at", "collected_at"),
        Index("ix_items_importance_score", "importance_score"),
    )

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Stable external identifier (UUID v4 string) for public API use.
    # The integer PK is used for all internal references.
    uuid: Mapped[str] = mapped_column(String(36), nullable=False)

    # Source identity
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # State machine
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="COLLECTED")
    failed_at_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Deduplication
    duplicate_of_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("items.id"), nullable=True
    )
    is_semantic_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Ranking
    importance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    # Pipeline metadata
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="EN")
    auto_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Extracted metadata (populated by normalize())
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    authors: Mapped[str | None] = mapped_column(Text, nullable=True)         # JSON list
    institutions: Mapped[str | None] = mapped_column(Text, nullable=True)    # JSON list
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    additional_urls: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    published_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    topics: Mapped[str | None] = mapped_column(Text, nullable=True)          # JSON list
    source_signals: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON dict

    # Embedding tracking (ANN index file is the sole vector store)
    embedding_computed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    embedding_model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Stage timestamps (NULL until that stage runs)
    collected_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
    normalized_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ranked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    enriched_at: Mapped[datetime | None] = mapped_column(nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    review_submitted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Relationships
    generated_contents: Mapped[list[GeneratedContent]] = relationship(
        "GeneratedContent", back_populates="item", cascade="all, delete-orphan"
    )
    review_records: Mapped[list[ReviewRecord]] = relationship(
        "ReviewRecord", back_populates="item", cascade="all, delete-orphan"
    )
    publishing_records: Mapped[list[PublishingRecord]] = relationship(
        "PublishingRecord", back_populates="item", cascade="all, delete-orphan"
    )
    raw_payloads: Mapped[list[RawSourcePayload]] = relationship(
        "RawSourcePayload", back_populates="item", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Item id={self.id} status={self.status} source={self.source_id}>"


# ---------------------------------------------------------------------------
# generated_content
# ---------------------------------------------------------------------------


class GeneratedContent(Base):
    """One LLM generation attempt for an item.

    An item may have multiple rows here (multiple attempts, or regeneration
    after reviewer rejection). Exactly one row per item has is_active=True
    at any given time — that is the content selected for review and publishing.
    """

    __tablename__ = "generated_content"

    __table_args__ = (
        Index("ix_generated_content_item_active", "item_id", "is_active"),
        Index("ix_generated_content_item_attempt", "item_id", "attempt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id"), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    prompt_template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    full_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    raw_output: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_segments: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON
    full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    validation_errors: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    llm_backend_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    # Relationships
    item: Mapped[Item] = relationship("Item", back_populates="generated_contents")
    review_records: Mapped[list[ReviewRecord]] = relationship(
        "ReviewRecord", back_populates="content"
    )
    publishing_records: Mapped[list[PublishingRecord]] = relationship(
        "PublishingRecord", back_populates="content"
    )

    def __repr__(self) -> str:
        return (
            f"<GeneratedContent id={self.id} item_id={self.item_id} "
            f"attempt={self.attempt} active={self.is_active}>"
        )


# ---------------------------------------------------------------------------
# review_records
# ---------------------------------------------------------------------------


class ReviewRecord(Base):
    """One human review submission for a generated content piece.

    Written when an item is submitted to the reviewer (GENERATED → PENDING_REVIEW).
    Updated with the decision (decided_at, decision, reviewer_note) when the
    reviewer responds.

    On restart, this table is queried to find pending reviews — the system does
    NOT re-submit items; it simply resumes polling for existing messages.
    """

    __tablename__ = "review_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id"), nullable=False)
    content_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("generated_content.id"), nullable=False
    )
    reviewer_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    tracking_id: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    item: Mapped[Item] = relationship("Item", back_populates="review_records")
    content: Mapped[GeneratedContent] = relationship(
        "GeneratedContent", back_populates="review_records"
    )

    def __repr__(self) -> str:
        return (
            f"<ReviewRecord id={self.id} item_id={self.item_id} "
            f"decision={self.decision}>"
        )


# ---------------------------------------------------------------------------
# publishing_records
# ---------------------------------------------------------------------------


class PublishingRecord(Base):
    """One publishing attempt for a content piece to a specific publisher.

    A separate row exists per item × publisher combination. An item is
    considered fully published when at least one publisher has status='PUBLISHED'.
    Individual publisher failures are tracked here for retry on the next run.
    """

    __tablename__ = "publishing_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id"), nullable=False)
    content_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("generated_content.id"), nullable=False
    )
    publisher_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    item: Mapped[Item] = relationship("Item", back_populates="publishing_records")
    content: Mapped[GeneratedContent] = relationship(
        "GeneratedContent", back_populates="publishing_records"
    )

    def __repr__(self) -> str:
        return (
            f"<PublishingRecord id={self.id} item_id={self.item_id} "
            f"publisher={self.publisher_id} status={self.status}>"
        )


# ---------------------------------------------------------------------------
# pipeline_runs
# ---------------------------------------------------------------------------


class PipelineRun(Base):
    """Audit log for each pipeline execution.

    Written at the start of every run; updated on completion or failure.
    On startup, any RUNNING record from a previous session indicates a crash
    and is marked FAILED by the RecoveryService before a new run starts.
    """

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    stage_metrics: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<PipelineRun id={self.id} status={self.status}>"


# ---------------------------------------------------------------------------
# raw_source_payloads — separated from items to keep items table lean
# ---------------------------------------------------------------------------


class RawSourcePayload(Base):
    """Original API response for each collected item.

    Written once by the collection stage; never updated. Enables replaying
    the normalization stage without re-fetching from the source.
    """

    __tablename__ = "raw_source_payloads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fetch_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    fetched_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    # Relationships
    item: Mapped[Item] = relationship("Item", back_populates="raw_payloads")

    def __repr__(self) -> str:
        return f"<RawSourcePayload id={self.id} item_id={self.item_id} source={self.source_id}>"
