"""
Unit tests for CollectStage.

Covers the Phase 2 quality gate items that Batch 7 makes evaluable, as
corrected by SDS §8.3:

  - exact deduplication against the DB (§8, Phase 2 deliverable; §8.3.1)
  - a source failure leaves the remaining sources running (§8.3.2)
  - a normalization failure marks that item FAILED and collection continues
    with the remaining payloads (§8.3.2, §3.3)
  - one raw_source_payloads row per item (§4.7)

Tested against an in-memory SQLite database, following the convention in
tests/unit/test_repositories.py. No HTTP: sources are stubs that return canned
payloads, so respx is not needed here.

The registry is a test double rather than a real SourceRegistry. A real one
discovers every BaseSource subclass in the interpreter, which would make these
tests call fetch() on the five production sources and hit the network.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from arip.config import SourceConfig
from arip.db.database import init_db
from arip.db.models import Item
from arip.db.models import RawSourcePayload as RawPayloadRow
from arip.db.repositories.items import ItemRepository
from arip.db.repositories.pipeline_runs import PipelineRunRepository
from arip.db.repositories.raw_payloads import RawPayloadRepository
from arip.entities import NormalizedItem, RawSourcePayload
from arip.enums import ItemStatus, SourceType
from arip.exceptions import SourceError
from arip.interfaces import BaseSource
from arip.pipeline.stages.collect import CollectStage
from arip.sources._http import compute_content_hash
from arip.state_machine import StateMachine

RUN_ID = 1


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubSource(BaseSource):
    """A source returning canned payloads, with controllable failure modes.

    Defined at module level so it behaves like a real plugin; it is always
    constructed directly by these tests, never discovered by a registry.
    """

    source_id = "_collect_stub"
    source_type = SourceType.PAPER

    def __init__(
        self,
        payloads: list[RawSourcePayload] | None = None,
        *,
        source_id: str | None = None,
        fetch_error: Exception | None = None,
        normalize_error: Exception | None = None,
        fail_external_ids: set[str] | None = None,
    ) -> None:
        super().__init__(None)
        self._payloads = payloads or []
        self._fetch_error = fetch_error
        self._normalize_error = normalize_error
        self._fail_external_ids = fail_external_ids or set()
        if source_id is not None:
            # Per-instance override so two stubs can act as different sources.
            self.source_id = source_id

    def fetch(self) -> list[RawSourcePayload]:
        if self._fetch_error is not None:
            raise self._fetch_error
        return list(self._payloads)

    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:
        if self._normalize_error is not None:
            raise self._normalize_error
        if payload.external_id in self._fail_external_ids:
            raise SourceError(f"missing required field for {payload.external_id}")
        return _normalized_from(payload)

    @classmethod
    def get_config_schema(cls) -> type:
        return SourceConfig


class _Registry:
    """Minimal stand-in for SourceRegistry.get_active_sources()."""

    def __init__(self, *sources: BaseSource) -> None:
        self._sources = list(sources)

    def get_active_sources(self) -> list[BaseSource]:
        return list(self._sources)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_payload(
    external_id: str = "2401.12345",
    source_id: str = "_collect_stub",
    title: str = "A Paper About Things",
    **raw: object,
) -> RawSourcePayload:
    data: dict[str, object] = {"title": title, "url": f"https://example.org/{external_id}"}
    data.update(raw)
    return RawSourcePayload(
        source_id=source_id,
        source_type=SourceType.PAPER,
        external_id=external_id,
        raw_data=data,
        fetched_at=datetime(2026, 8, 24, 12, 0, 0),
    )


def _normalized_from(payload: RawSourcePayload) -> NormalizedItem:
    """Build a NormalizedItem the way a real source's normalize() would."""
    data = payload.raw_data
    title = str(data.get("title", ""))
    return NormalizedItem(
        source_id=payload.source_id,
        source_type=payload.source_type.value,
        external_id=payload.external_id,
        content_hash=compute_content_hash(
            source_id=payload.source_id,
            external_id=payload.external_id,
            title=title,
        ),
        language="EN",
        title=title,
        primary_url=str(data.get("url", "")),
        raw_payload=json.dumps(payload.raw_data),
        authors=data.get("authors"),
        institutions=data.get("institutions"),
        abstract=data.get("abstract"),
        additional_urls=data.get("additional_urls"),
        published_date=data.get("published_date"),
        topics=data.get("topics"),
        source_signals=data.get("source_signals"),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def item_repo(db_session: Session) -> ItemRepository:
    return ItemRepository(db_session)


@pytest.fixture
def make_stage(db_session: Session, item_repo: ItemRepository):
    """Return a factory that builds a CollectStage over the given sources."""

    def _factory(*sources: BaseSource) -> CollectStage:
        return CollectStage(
            registry=_Registry(*sources),
            item_repo=item_repo,
            payload_repo=RawPayloadRepository(db_session),
            state_machine=StateMachine(item_repo),
        )

    return _factory


def all_items(session: Session) -> list[Item]:
    return list(session.query(Item).order_by(Item.id).all())


def all_payload_rows(session: Session) -> list[RawPayloadRow]:
    return list(session.query(RawPayloadRow).order_by(RawPayloadRow.id).all())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_collects_payload_into_item(make_stage, db_session: Session) -> None:
    """A normalizable payload becomes one item row."""
    make_stage(_StubSource([make_payload()])).run(RUN_ID)

    items = all_items(db_session)
    assert len(items) == 1
    assert items[0].external_id == "2401.12345"


def test_collected_item_is_in_collected_status(make_stage, db_session: Session) -> None:
    """Items are left in COLLECTED — ranking is a later batch (SDS §8.2)."""
    make_stage(_StubSource([make_payload()])).run(RUN_ID)

    assert all_items(db_session)[0].status == ItemStatus.COLLECTED.value


def test_collects_from_every_active_source(make_stage, db_session: Session) -> None:
    """Each active source contributes its payloads."""
    a = _StubSource([make_payload("a1", source_id="src_a")], source_id="src_a")
    b = _StubSource([make_payload("b1", source_id="src_b")], source_id="src_b")

    make_stage(a, b).run(RUN_ID)

    assert {i.source_id for i in all_items(db_session)} == {"src_a", "src_b"}


def test_all_payloads_collected_no_item_cap(make_stage, db_session: Session) -> None:
    """max_items_per_run is not applied at collection — deferred to Phase 3 (§9.2)."""
    payloads = [make_payload(f"p{n}") for n in range(7)]

    make_stage(_StubSource(payloads)).run(RUN_ID)

    assert len(all_items(db_session)) == 7


# ---------------------------------------------------------------------------
# Field mapping — NormalizedItem to items columns
# ---------------------------------------------------------------------------


def test_list_fields_are_stored_as_json(make_stage, db_session: Session) -> None:
    """List-valued fields are serialized into their TEXT columns (§4.2)."""
    payload = make_payload(
        authors=["Ada Lovelace", "Alan Turing"],
        topics=["cs.AI", "cs.LG"],
    )
    make_stage(_StubSource([payload])).run(RUN_ID)

    item = all_items(db_session)[0]
    assert json.loads(item.authors) == ["Ada Lovelace", "Alan Turing"]
    assert json.loads(item.topics) == ["cs.AI", "cs.LG"]


def test_source_signals_dict_is_stored_as_json(make_stage, db_session: Session) -> None:
    """Dict-valued source_signals is serialized rather than passed through."""
    make_stage(_StubSource([make_payload(source_signals={"stars": 42})])).run(RUN_ID)

    assert json.loads(all_items(db_session)[0].source_signals) == {"stars": 42}


def test_absent_optional_fields_stay_null(make_stage, db_session: Session) -> None:
    """None survives the mapping as NULL rather than becoming the string 'null'."""
    make_stage(_StubSource([make_payload()])).run(RUN_ID)

    item = all_items(db_session)[0]
    assert item.authors is None
    assert item.topics is None
    assert item.source_signals is None


def test_published_date_string_becomes_a_date(make_stage, db_session: Session) -> None:
    """The ISO string from normalize() is converted for the DATE column.

    Guards a trap of the same family as TD-011: every source emits
    published_date as 'YYYY-MM-DD', but the column accepts only date objects.
    Passing the string through raises StatementError.
    """
    make_stage(_StubSource([make_payload(published_date="2024-01-15")])).run(RUN_ID)

    assert all_items(db_session)[0].published_date == date(2024, 1, 15)


def test_empty_published_date_is_null(make_stage, db_session: Session) -> None:
    """Sources emit '' when the source system gave no date; that is not an error."""
    make_stage(_StubSource([make_payload(published_date="")])).run(RUN_ID)

    assert all_items(db_session)[0].published_date is None


def test_unparseable_published_date_is_null_not_a_failure(
    make_stage, db_session: Session
) -> None:
    """A malformed date does not fail the item — §4.2 declares the column nullable."""
    make_stage(_StubSource([make_payload(published_date="not-a-date")])).run(RUN_ID)

    items = all_items(db_session)
    assert len(items) == 1
    assert items[0].published_date is None
    assert items[0].status == ItemStatus.COLLECTED.value


# ---------------------------------------------------------------------------
# Raw payload persistence — SDS §4.7
# ---------------------------------------------------------------------------


def test_one_raw_payload_row_per_item(make_stage, db_session: Session) -> None:
    """Every collected item gets its raw payload stored alongside."""
    make_stage(_StubSource([make_payload()])).run(RUN_ID)

    rows = all_payload_rows(db_session)
    assert len(rows) == 1
    assert rows[0].item_id == all_items(db_session)[0].id


def test_raw_payload_round_trips_as_json(make_stage, db_session: Session) -> None:
    """The stored payload parses back to the original API response (§4.7 replay)."""
    payload = make_payload(title="Braces {and} \"quotes\"")
    make_stage(_StubSource([payload])).run(RUN_ID)

    assert json.loads(all_payload_rows(db_session)[0].payload) == payload.raw_data


def test_raw_payload_records_fetch_time(make_stage, db_session: Session) -> None:
    """fetched_at comes from the payload, not from insertion time."""
    make_stage(_StubSource([make_payload()])).run(RUN_ID)

    assert all_payload_rows(db_session)[0].fetched_at == datetime(2026, 8, 24, 12, 0, 0)


# ---------------------------------------------------------------------------
# Exact deduplication — SDS §8.3.1
# ---------------------------------------------------------------------------


def test_identical_payload_collected_twice_creates_one_item(
    make_stage, db_session: Session
) -> None:
    """Re-collecting the same payload creates no second row and does not raise."""
    stage = make_stage(_StubSource([make_payload()]))
    stage.run(RUN_ID)
    stage.run(RUN_ID + 1)

    assert len(all_items(db_session)) == 1


def test_duplicate_creates_no_second_raw_payload_row(
    make_stage, db_session: Session
) -> None:
    """A discarded duplicate writes nothing at all — it is dropped before insertion."""
    stage = make_stage(_StubSource([make_payload()]))
    stage.run(RUN_ID)
    stage.run(RUN_ID + 1)

    assert len(all_payload_rows(db_session)) == 1


def test_duplicate_does_not_change_item_status(make_stage, db_session: Session) -> None:
    """No state transition occurs for a duplicate (§8.3.1: DUPLICATE is unreachable here)."""
    stage = make_stage(_StubSource([make_payload()]))
    stage.run(RUN_ID)
    stage.run(RUN_ID + 1)

    assert all_items(db_session)[0].status == ItemStatus.COLLECTED.value


def test_retitled_item_is_not_collected_twice(make_stage, db_session: Session) -> None:
    """A revised title changes content_hash, but (source_id, external_id) still matches.

    Without the second check this would hit the UNIQUE(source_id, external_id)
    constraint declared in §4.2 and raise IntegrityError.
    """
    make_stage(_StubSource([make_payload(title="Original Title")])).run(RUN_ID)
    make_stage(_StubSource([make_payload(title="Corrected Title")])).run(RUN_ID + 1)

    items = all_items(db_session)
    assert len(items) == 1
    assert items[0].title == "Original Title"


def test_same_work_from_two_sources_creates_two_items(
    make_stage, db_session: Session
) -> None:
    """Cross-source duplicates are NOT exact duplicates (§8.3.1).

    content_hash includes source_id (§4.2), so the same paper fetched from two
    sources yields two distinct hashes and two rows. Detecting this pair is
    semantic deduplication's job in Phase 3. This test documents the
    consequence rather than asserting a desirable outcome.
    """
    title = "One Paper, Two Sources"
    a = _StubSource([make_payload("x1", source_id="src_a", title=title)], source_id="src_a")
    b = _StubSource([make_payload("x1", source_id="src_b", title=title)], source_id="src_b")

    make_stage(a, b).run(RUN_ID)

    assert len(all_items(db_session)) == 2


# ---------------------------------------------------------------------------
# Normalization failure — SDS §3.3, §8.3.2
# ---------------------------------------------------------------------------


def test_normalization_failure_marks_item_failed(make_stage, db_session: Session) -> None:
    """An unnormalizable payload produces an item in FAILED."""
    source = _StubSource([make_payload("bad")], fail_external_ids={"bad"})
    make_stage(source).run(RUN_ID)

    items = all_items(db_session)
    assert len(items) == 1
    assert items[0].status == ItemStatus.FAILED.value


def test_normalization_failure_records_stage_and_reason(
    make_stage, db_session: Session
) -> None:
    """failed_at_stage and failure_reason are populated per §3.3."""
    source = _StubSource([make_payload("bad")], fail_external_ids={"bad"})
    make_stage(source).run(RUN_ID)

    item = all_items(db_session)[0]
    assert item.failed_at_stage == "NORMALIZATION"
    assert "missing required field" in item.failure_reason


def test_normalization_failure_increments_retry_count(
    make_stage, db_session: Session
) -> None:
    """retry_count is incremented on the failure, per §3.3."""
    source = _StubSource([make_payload("bad")], fail_external_ids={"bad"})
    make_stage(source).run(RUN_ID)

    assert all_items(db_session)[0].retry_count == 1


def test_normalization_failure_stores_raw_payload(
    make_stage, db_session: Session
) -> None:
    """The payload is kept so the item can be re-normalized without re-fetching (§4.7)."""
    source = _StubSource([make_payload("bad")], fail_external_ids={"bad"})
    make_stage(source).run(RUN_ID)

    rows = all_payload_rows(db_session)
    assert len(rows) == 1
    assert json.loads(rows[0].payload)["title"] == "A Paper About Things"


def test_collection_continues_after_normalization_failure(
    make_stage, db_session: Session
) -> None:
    """The remaining payloads are still collected (§8.3.2, second assertion)."""
    payloads = [make_payload("good1"), make_payload("bad"), make_payload("good2")]
    source = _StubSource(payloads, fail_external_ids={"bad"})

    make_stage(source).run(RUN_ID)

    by_status = {i.external_id: i.status for i in all_items(db_session)}
    assert by_status["good1"] == ItemStatus.COLLECTED.value
    assert by_status["good2"] == ItemStatus.COLLECTED.value
    assert by_status["bad"] == ItemStatus.FAILED.value


def test_repeated_normalization_failure_creates_one_item(
    make_stage, db_session: Session
) -> None:
    """A payload that fails again on a later run does not insert a second row."""
    source = _StubSource([make_payload("bad")], fail_external_ids={"bad"})
    stage = make_stage(source)
    stage.run(RUN_ID)
    stage.run(RUN_ID + 1)

    assert len(all_items(db_session)) == 1


def test_unexpected_normalize_exception_is_contained(
    make_stage, db_session: Session
) -> None:
    """A plugin bug raising something other than SourceError still fails only its item."""
    source = _StubSource(
        [make_payload("boom")], normalize_error=KeyError("unexpected key")
    )
    make_stage(source).run(RUN_ID)

    assert all_items(db_session)[0].status == ItemStatus.FAILED.value


# ---------------------------------------------------------------------------
# Source failure isolation — SDS §5.2, §5.3, §8.3.2
# ---------------------------------------------------------------------------


def test_empty_fetch_creates_no_items(make_stage, db_session: Session) -> None:
    """A source returning [] after an HTTP failure contributes nothing (§5.3)."""
    make_stage(_StubSource([])).run(RUN_ID)

    assert all_items(db_session) == []


def test_failing_source_does_not_stop_other_sources(
    make_stage, db_session: Session
) -> None:
    """§8.3.2, first assertion: the remaining sources still run."""
    broken = _StubSource([], source_id="src_broken", fetch_error=RuntimeError("boom"))
    healthy = _StubSource([make_payload("ok", source_id="src_ok")], source_id="src_ok")

    make_stage(broken, healthy).run(RUN_ID)

    items = all_items(db_session)
    assert len(items) == 1
    assert items[0].source_id == "src_ok"


def test_failing_source_creates_no_item(make_stage, db_session: Session) -> None:
    """An HTTP failure yields no payloads, so there is no item to mark FAILED (§8.3.2)."""
    make_stage(_StubSource([], fetch_error=RuntimeError("boom"))).run(RUN_ID)

    assert all_items(db_session) == []


def test_run_with_no_active_sources_is_a_no_op(make_stage, db_session: Session) -> None:
    """An empty registry is not an error."""
    make_stage().run(RUN_ID)

    assert all_items(db_session) == []


# ---------------------------------------------------------------------------
# Session ownership — SDS §5.14
# ---------------------------------------------------------------------------


def test_stage_does_not_commit_the_session(make_stage, db_session: Session) -> None:
    """The stage flushes but never commits — the orchestrator owns the transaction."""
    make_stage(_StubSource([make_payload()])).run(RUN_ID)

    assert db_session.in_transaction()


def test_collection_shares_the_run_session(
    make_stage, db_session: Session
) -> None:
    """Items and the pipeline_runs row are written through the same session."""
    run = PipelineRunRepository(db_session).create()
    make_stage(_StubSource([make_payload()])).run(run.id)

    assert len(all_items(db_session)) == 1
    assert run.id is not None


# ---------------------------------------------------------------------------
# normalized_at — SDS §8.5
# ---------------------------------------------------------------------------


def test_collected_item_has_normalized_at(make_stage, db_session: Session) -> None:
    """CollectStage stamps normalized_at, per SDS §8.5.

    §3.3's action line attributes this to the COLLECTED -> RANKED transition,
    but §8.5 records that ranking and normalization are separate stages and
    this is the one that normalizes.
    """
    make_stage(_StubSource([make_payload()])).run(RUN_ID)

    assert isinstance(all_items(db_session)[0].normalized_at, datetime)


def test_normalization_failure_leaves_normalized_at_null(
    make_stage, db_session: Session
) -> None:
    """The stamp records that normalization succeeded, so a failure has none."""
    source = _StubSource([make_payload("bad")], fail_external_ids={"bad"})
    make_stage(source).run(RUN_ID)

    item = all_items(db_session)[0]
    assert item.status == ItemStatus.FAILED.value
    assert item.normalized_at is None
