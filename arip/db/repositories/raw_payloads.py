"""
RawPayloadRepository — all database access for the raw_source_payloads table.

No base class. Plain Python class with exactly the methods its callers need
(SDS §5.14.1, AD-10). Testing: pass a Session connected to an in-memory SQLite
database.

Every method operates within the caller's session. The caller is responsible
for committing or rolling back. RawPayloadRepository never commits or closes
the session.

Why this table exists separately from ``items``: SDS §4.7 keeps the primary
lookup table lean by storing the original API response in its own table,
written once and never updated. This enables replay of the normalization stage
without re-fetching. The ``items`` table carries no ``raw_payload`` column —
see TD-011 for the SDS §4.2 / §4.7 contradiction this resolves.

Note on naming: ``RawSourcePayload`` is both an ORM model (``arip.db.models``)
and a transport dataclass (``arip.entities``). This repository deals in the
ORM model only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from arip.db.models import RawSourcePayload

logger = structlog.get_logger(__name__)


class RawPayloadRepository:
    """Data access object for the raw_source_payloads table.

    JSON serialization of the ``payload`` column is the responsibility of the
    caller — this class treats it as an opaque string, exactly as
    ``ItemRepository`` treats the JSON columns on ``items``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, data: dict[str, Any]) -> RawSourcePayload:
        """Insert a raw payload row and return the persisted record.

        Rows in this table are written once and never updated (SDS §4.7), so
        no update or delete method is provided.

        Args:
            data: Dict of column values. Must include ``item_id``,
                  ``source_id`` and ``payload`` (a JSON string).
                  ``fetch_url`` is optional and is left NULL when absent —
                  see TD-012. ``fetched_at`` defaults to the current UTC time.

        Returns:
            The newly created RawSourcePayload with its auto-assigned id
            populated.
        """
        if "fetched_at" not in data:
            data["fetched_at"] = datetime.utcnow()

        payload = RawSourcePayload(**data)
        self._session.add(payload)
        self._session.flush()  # Populate payload.id without committing.
        logger.debug(
            "raw_payload_created",
            payload_id=payload.id,
            item_id=payload.item_id,
            source_id=payload.source_id,
        )
        return payload
