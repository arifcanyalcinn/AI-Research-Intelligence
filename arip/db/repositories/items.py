"""
ItemRepository — all database access for the items table.

No base class. Plain Python class with exactly the methods its callers need.
Testing: pass a Session connected to an in-memory SQLite database.

Every method operates within the caller's session. The caller is responsible
for committing or rolling back. ItemRepository never commits or closes the session.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from arip.db.models import Item
from arip.enums import ItemStatus

logger = structlog.get_logger(__name__)


class ItemRepository:
    """Data access object for the items table.

    All methods accept and return ORM objects. JSON serialization of list/dict
    columns is the responsibility of the caller — this class treats those
    columns as opaque strings.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create(self, data: dict[str, Any]) -> Item:
        """Insert a new item row and return the persisted Item.

        Args:
            data: Dict of column values. Must include source_id, source_type,
                  external_id, content_hash, title, primary_url, raw_payload.
                  uuid is auto-generated if not provided.

        Returns:
            The newly created Item with its auto-assigned id populated.

        Raises:
            IntegrityError: If the content_hash or source_id+external_id
                            already exists (exact duplicate — caller should
                            handle this as a deduplication hit).
        """
        if "uuid" not in data:
            data["uuid"] = str(uuid.uuid4())
        if "collected_at" not in data:
            data["collected_at"] = datetime.utcnow()
        if "updated_at" not in data:
            data["updated_at"] = datetime.utcnow()
        if "status" not in data:
            data["status"] = ItemStatus.COLLECTED.value

        item = Item(**data)
        self._session.add(item)
        self._session.flush()  # Populate item.id without committing.
        logger.debug("item_created", item_id=item.id, source_id=item.source_id)
        return item

    def update_status(
        self,
        item_id: int,
        status: ItemStatus,
        **fields: Any,
    ) -> None:
        """Update the status of an item and optionally additional fields.

        Called exclusively by StateMachine.transition() — not by stage code directly.

        Args:
            item_id: Primary key of the item to update.
            status: The new ItemStatus value.
            **fields: Additional column updates (e.g. failed_at_stage, failure_reason,
                      importance_score, normalized_at, etc.).
        """
        item = self._session.get(Item, item_id)
        if item is None:
            logger.error("update_status_item_not_found", item_id=item_id)
            return

        old_status = item.status
        item.status = status.value
        item.updated_at = datetime.utcnow()

        for field_name, value in fields.items():
            if hasattr(item, field_name):
                setattr(item, field_name, value)
            else:
                logger.warning(
                    "update_status_unknown_field",
                    item_id=item_id,
                    field=field_name,
                )

        self._session.flush()
        logger.debug(
            "item_status_updated",
            item_id=item_id,
            old_status=old_status,
            new_status=status.value,
        )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_by_id(self, item_id: int) -> Item | None:
        """Fetch a single item by its integer primary key.

        Returns:
            The Item if found, or None.
        """
        return self._session.get(Item, item_id)

    def get_by_content_hash(self, content_hash: str) -> Item | None:
        """Look up an item by its content hash (exact deduplication check).

        Returns:
            The Item if found, or None.
        """
        stmt = select(Item).where(Item.content_hash == content_hash)
        return self._session.execute(stmt).scalar_one_or_none()

    def get_by_status(self, status: ItemStatus) -> list[Item]:
        """Fetch all items with the given status, ordered by collected_at.

        Args:
            status: The ItemStatus to filter on.

        Returns:
            List of Items, oldest first.
        """
        stmt = (
            select(Item)
            .where(Item.status == status.value)
            .order_by(Item.collected_at.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def get_by_status_and_language(
        self, status: ItemStatus, language: str
    ) -> list[Item]:
        """Fetch items by status and language, ordered by importance_score descending.

        Used by generation and publishing stages to select items for processing.

        Args:
            status: The ItemStatus to filter on.
            language: The language code (e.g. 'EN').

        Returns:
            List of Items, highest-scored first.
        """
        stmt = (
            select(Item)
            .where(Item.status == status.value, Item.language == language)
            .order_by(Item.importance_score.desc().nulls_last())
        )
        return list(self._session.execute(stmt).scalars().all())

    def get_by_source_and_external_id(
        self, source_id: str, external_id: str
    ) -> Item | None:
        """Look up an item by its source identifier and external ID.

        Args:
            source_id: The source plugin ID (e.g. 'arxiv').
            external_id: The source-native item identifier.

        Returns:
            The Item if found, or None.
        """
        stmt = select(Item).where(
            Item.source_id == source_id,
            Item.external_id == external_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def get_pending_review(self, timeout_hours: int) -> list[Item]:
        """Fetch items in PENDING_REVIEW that have exceeded the review timeout.

        Used by the timeout scan at the start of each pipeline run.

        Args:
            timeout_hours: How many hours to wait before treating a review as timed out.

        Returns:
            List of Items that should be transitioned to FAILED(REVIEW_TIMEOUT).
        """
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(hours=timeout_hours)
        stmt = (
            select(Item)
            .where(
                Item.status == ItemStatus.PENDING_REVIEW.value,
                Item.review_submitted_at < cutoff,
            )
        )
        return list(self._session.execute(stmt).scalars().all())

    def get_all_with_embeddings(self) -> list[Item]:
        """Fetch all items that have been embedded and are not terminal-failed.

        Used by the ANN index reconciliation on startup to re-embed any items
        that were added to the DB but are missing from the index file.

        Returns:
            List of Items with embedding_computed_at IS NOT NULL and status
            not in (DUPLICATE, FILTERED, FAILED).
        """
        excluded_statuses = {
            ItemStatus.DUPLICATE.value,
            ItemStatus.FILTERED.value,
            ItemStatus.FAILED.value,
        }
        stmt = select(Item).where(
            Item.embedding_computed_at.is_not(None),
            Item.status.not_in(excluded_statuses),
        )
        return list(self._session.execute(stmt).scalars().all())
