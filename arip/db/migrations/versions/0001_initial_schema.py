"""Initial schema — all six tables.

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-07-18 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # items — central table
    # ------------------------------------------------------------------
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="COLLECTED"),
        sa.Column("failed_at_stage", sa.String(length=32), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_of_id", sa.Integer(), sa.ForeignKey("items.id"), nullable=True),
        sa.Column("is_semantic_duplicate", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("importance_score", sa.Float(), nullable=True),
        sa.Column("signal_breakdown", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=False, server_default="EN"),
        sa.Column("auto_approved", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("authors", sa.Text(), nullable=True),
        sa.Column("institutions", sa.Text(), nullable=True),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("primary_url", sa.Text(), nullable=True),
        sa.Column("additional_urls", sa.Text(), nullable=True),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.Column("topics", sa.Text(), nullable=True),
        sa.Column("source_signals", sa.Text(), nullable=True),
        sa.Column("embedding_computed_at", sa.DateTime(), nullable=True),
        sa.Column("embedding_model_name", sa.String(length=128), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("normalized_at", sa.DateTime(), nullable=True),
        sa.Column("ranked_at", sa.DateTime(), nullable=True),
        sa.Column("enriched_at", sa.DateTime(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("review_submitted_at", sa.DateTime(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_items"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_items_source_external"),
        sa.UniqueConstraint("content_hash", name="uq_items_content_hash"),
    )
    op.create_index("ix_items_status", "items", ["status"])
    op.create_index("ix_items_collected_at", "items", ["collected_at"])
    op.create_index("ix_items_importance_score", "items", ["importance_score"])

    # ------------------------------------------------------------------
    # generated_content
    # ------------------------------------------------------------------
    op.create_table(
        "generated_content",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id"), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("prompt_template_id", sa.String(length=64), nullable=False),
        sa.Column("full_prompt", sa.Text(), nullable=False),
        sa.Column("raw_output", sa.Text(), nullable=False),
        sa.Column("parsed_segments", sa.Text(), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=True),
        sa.Column("validation_passed", sa.Boolean(), nullable=False),
        sa.Column("validation_errors", sa.Text(), nullable=True),
        sa.Column("llm_backend_id", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("generation_latency_ms", sa.Integer(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_generated_content"),
    )
    op.create_index(
        "ix_generated_content_item_active", "generated_content", ["item_id", "is_active"]
    )
    op.create_index(
        "ix_generated_content_item_attempt", "generated_content", ["item_id", "attempt"]
    )

    # ------------------------------------------------------------------
    # review_records
    # ------------------------------------------------------------------
    op.create_table(
        "review_records",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id"), nullable=False),
        sa.Column(
            "content_id",
            sa.Integer(),
            sa.ForeignKey("generated_content.id"),
            nullable=False,
        ),
        sa.Column("reviewer_backend", sa.String(length=32), nullable=False),
        sa.Column("tracking_id", sa.String(length=128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_review_records"),
    )

    # ------------------------------------------------------------------
    # publishing_records
    # ------------------------------------------------------------------
    op.create_table(
        "publishing_records",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id"), nullable=False),
        sa.Column(
            "content_id",
            sa.Integer(),
            sa.ForeignKey("generated_content.id"),
            nullable=False,
        ),
        sa.Column("publisher_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_publishing_records"),
    )

    # ------------------------------------------------------------------
    # pipeline_runs
    # ------------------------------------------------------------------
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="RUNNING"),
        sa.Column("stage_metrics", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_runs"),
    )

    # ------------------------------------------------------------------
    # raw_source_payloads
    # ------------------------------------------------------------------
    op.create_table(
        "raw_source_payloads",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id"), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("fetch_url", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_raw_source_payloads"),
    )


def downgrade() -> None:
    # Drop in reverse dependency order.
    op.drop_table("raw_source_payloads")
    op.drop_table("pipeline_runs")
    op.drop_table("publishing_records")
    op.drop_table("review_records")
    op.drop_table("generated_content")
    op.drop_index("ix_items_importance_score", table_name="items")
    op.drop_index("ix_items_collected_at", table_name="items")
    op.drop_index("ix_items_status", table_name="items")
    op.drop_table("items")
