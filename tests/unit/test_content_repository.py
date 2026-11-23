"""
Unit tests for ContentRepository (SDS 5.14, D-015).

In-memory SQLite. The repository never commits; these tests do not either,
except where a test needs a fresh query.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from arip.db.database import init_db
from arip.db.repositories.content import ContentRepository
from arip.db.repositories.items import ItemRepository
from arip.enums import ItemStatus


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    yield db
    db.close()
    engine.dispose()


def make_item(session: Session, name: str) -> int:
    item = ItemRepository(session).create(
        {
            "source_id": "arxiv",
            "source_type": "PAPER",
            "external_id": name,
            "content_hash": f"hash-{name}",
            "status": ItemStatus.ENRICHED.value,
            "title": name,
            "primary_url": f"https://arxiv.org/abs/{name}",
        }
    )
    return item.id


def row(item_id: int, attempt: int, active: bool) -> dict:
    return {
        "item_id": item_id,
        "language": "EN",
        "attempt": attempt,
        "is_active": active,
        "prompt_template_id": "thread_en",
        "full_prompt": "### SYSTEM\ns\n\n### USER\nu",
        "raw_output": "raw",
        "parsed_segments": None,
        "full_text": None,
        "validation_passed": active,
        "validation_errors": "[]",
        "llm_backend_id": "stub",
        "model_name": "stub:m",
        "temperature": 0.7,
        "max_tokens": 1024,
        "seed": attempt,
        "generation_latency_ms": 0,
        "generated_at": datetime(2026, 11, 23, 12, 0, 0),
    }


def test_create_returns_persisted_row(session: Session) -> None:
    item_id = make_item(session, "1")

    created = ContentRepository(session).create(row(item_id, 1, False))

    assert created.id is not None
    assert session.get(type(created), created.id) is created


def test_get_active_ignores_inactive(session: Session) -> None:
    item_id = make_item(session, "1")
    repo = ContentRepository(session)
    repo.create(row(item_id, 1, False))

    assert repo.get_active_for_item(item_id) is None
    active = repo.create(row(item_id, 2, True))
    assert repo.get_active_for_item(item_id) is active


def test_deactivate_all_is_scoped_to_item(session: Session) -> None:
    first, second = make_item(session, "1"), make_item(session, "2")
    repo = ContentRepository(session)
    a = repo.create(row(first, 1, True))
    b = repo.create(row(second, 1, True))

    repo.deactivate_all_for_item(first)

    assert a.is_active is False
    assert b.is_active is True
    assert repo.get_active_for_item(first) is None
    assert repo.get_active_for_item(second) is b


def test_get_by_item_and_attempt(session: Session) -> None:
    first, second = make_item(session, "1"), make_item(session, "2")
    repo = ContentRepository(session)
    wanted = repo.create(row(first, 2, False))
    repo.create(row(first, 1, False))
    repo.create(row(second, 2, False))

    assert repo.get_by_item_and_attempt(first, 2) is wanted
    assert repo.get_by_item_and_attempt(first, 3) is None


def test_max_attempt_for_item(session: Session) -> None:
    first, second = make_item(session, "1"), make_item(session, "2")
    repo = ContentRepository(session)

    assert repo.max_attempt_for_item(first) == 0
    for attempt in (1, 3, 2):
        repo.create(row(first, attempt, False))
    repo.create(row(second, 9, False))

    assert repo.max_attempt_for_item(first) == 3
    assert repo.max_attempt_for_item(second) == 9
