"""
ContentRepository — all database access for the generated_content table (SDS 5.14).

No base class. Plain Python class with exactly the methods its callers need.
Testing: pass a Session connected to an in-memory SQLite database.

Every method operates within the caller's session. The caller is responsible
for committing or rolling back. ContentRepository never commits or closes the
session. The JSON columns (parsed_segments, validation_errors) are opaque
strings here; the generator serialises them (D-014, D-015).

``max_attempt_for_item()`` is an addition beside the four methods SDS 5.14
lists: the generator numbers a new pass's attempts after the item's highest
stored attempt (ruling A6, D-015).
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from arip.db.models import GeneratedContent

logger = structlog.get_logger(__name__)


class ContentRepository:
    """Data access object for the generated_content table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create(self, data: dict[str, Any]) -> GeneratedContent:
        """Insert one generated_content row and return it with its id populated."""
        content = GeneratedContent(**data)
        self._session.add(content)
        self._session.flush()  # Populate content.id without committing.
        logger.debug(
            "generated_content_created",
            content_id=content.id,
            item_id=content.item_id,
            attempt=content.attempt,
            is_active=content.is_active,
        )
        return content

    def deactivate_all_for_item(self, item_id: int) -> None:
        """Set is_active = False on every row of ``item_id``."""
        self._session.execute(
            update(GeneratedContent)
            .where(GeneratedContent.item_id == item_id, GeneratedContent.is_active.is_(True))
            .values(is_active=False)
            .execution_options(synchronize_session="fetch")
        )
        self._session.flush()

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_active_for_item(self, item_id: int) -> GeneratedContent | None:
        """The active row of ``item_id``, or None.

        At most one row is active (D-015). Should there be more, the highest
        attempt is returned and an error is logged.
        """
        rows = list(
            self._session.execute(
                select(GeneratedContent)
                .where(GeneratedContent.item_id == item_id, GeneratedContent.is_active.is_(True))
                .order_by(GeneratedContent.attempt.desc())
            )
            .scalars()
            .all()
        )
        if len(rows) > 1:
            logger.error("generated_content_several_active", item_id=item_id, count=len(rows))
        return rows[0] if rows else None

    def get_by_item_and_attempt(self, item_id: int, attempt: int) -> GeneratedContent | None:
        """The row of ``item_id`` with this attempt number, or None."""
        return self._session.execute(
            select(GeneratedContent).where(
                GeneratedContent.item_id == item_id, GeneratedContent.attempt == attempt
            )
        ).scalar_one_or_none()

    def max_attempt_for_item(self, item_id: int) -> int:
        """The highest stored attempt number of ``item_id``; 0 when it has none."""
        value = self._session.execute(
            select(func.max(GeneratedContent.attempt)).where(GeneratedContent.item_id == item_id)
        ).scalar()
        return int(value) if value is not None else 0
