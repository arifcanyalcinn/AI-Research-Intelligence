"""
Unit tests for HuggingFacePapersSource.

SDS §5.3 testing strategy for sources:
  "Mock HTTP responses using respx (for httpx).
   Test: successful fetch, network timeout, 429 rate limit, malformed response.
   No network calls in tests."

Every test in this file is strictly offline — all HTTP is intercepted by respx
at the httpx transport layer.  respx raises ``RuntimeError`` if any unmocked
request is attempted.

RESPX WORKAROUND (issue #277, pinned respx 0.21.1):
  Use ``mock.route(url__startswith=URL)`` instead of ``mock.get(URL)``.
  See tests/unit/sources/conftest.py for the full explanation.

Test coverage:
  - class attributes: source_id, source_type, get_config_schema.
  - fetch(): single entry, two entries, empty array, malformed JSON,
    non-list JSON, network timeout (retried 3×, returns []),
    HTTP 429 (retried 3×, returns []), HTTP 401 (1 attempt, returns []),
    HTTP 500 (retried 3×, returns []).
  - normalize(): title, abstract, authors, primary_url, additional_urls,
    published_date, source_signals, content_hash, language, source_type value,
    institutions=None, topics=None.
  - normalize(): SourceError on missing title (empty and absent key).
  - normalize(): SourceError on missing external_id / primary_url.
  - normalize(): empty authors → None, empty abstract → None,
    empty published_date → None.
  - normalize(): content_hash matches SDS §4.2 formula.
  - normalize(): raw_payload is JSON-serialised raw_data.
  - payload structure: external_id, title, authors, abstract, published_date,
    upvotes all extracted correctly from the nested API shape.
  - registry integration: SourceRegistry discovers HuggingFacePapersSource;
    appears in get_active_sources(); excluded when enabled=False.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from arip.config import SourceConfig, load_settings
from arip.entities import RawSourcePayload
from arip.enums import SourceType
from arip.exceptions import SourceError
from arip.sources._http import compute_content_hash
from arip.sources.huggingface_papers import (
    HF_DAILY_PAPERS_URL,
    HuggingFacePapersSource,
)
from arip.sources.registry import SourceRegistry

# ---------------------------------------------------------------------------
# JSON response fixtures
# ---------------------------------------------------------------------------

# Minimal valid single-paper response matching the real HuggingFace Papers API shape.
_SINGLE_ENTRY: dict = {
    "id": "2401.12345",
    "title": "Attention Is All You Need Again",
    "paper": {
        "id": "2401.12345",
        "title": "Attention Is All You Need Again",
        "authors": [
            {"name": "Alice Smith", "_id": "abc123", "hidden": False},
            {"name": "Bob Jones", "_id": "def456", "hidden": False},
        ],
        "publishedAt": "2024-01-15T00:00:00.000Z",
        "summary": "A new transformer architecture that changes everything.",
        "upvotes": 42,
        "discussionId": "disc123",
        "mediaUrls": [],
    },
    "publishedAt": "2024-01-15T00:00:00.000Z",
    "submittedOnDailyAt": "2024-01-15T08:00:00.000Z",
}

_SECOND_ENTRY: dict = {
    "id": "2401.67890",
    "title": "Scaling Laws for Neural Language Models",
    "paper": {
        "id": "2401.67890",
        "title": "Scaling Laws for Neural Language Models",
        "authors": [
            {"name": "Carol White", "_id": "ghi789", "hidden": False},
        ],
        "publishedAt": "2024-01-14T00:00:00.000Z",
        "summary": "A study of scaling behaviour in language models.",
        "upvotes": 17,
        "discussionId": "disc456",
        "mediaUrls": [],
    },
    "publishedAt": "2024-01-14T00:00:00.000Z",
    "submittedOnDailyAt": "2024-01-14T09:00:00.000Z",
}

SINGLE_ENTRY_RESPONSE: str = json.dumps([_SINGLE_ENTRY])
TWO_ENTRY_RESPONSE: str = json.dumps([_SINGLE_ENTRY, _SECOND_ENTRY])
EMPTY_RESPONSE: str = json.dumps([])
MALFORMED_JSON: str = "this is not json at all <<<"
NON_LIST_JSON: str = json.dumps({"error": "not a list"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_settings(tmp_path: Path):
    """Return an AppSettings with only the required llm.model_name field."""
    p = tmp_path / "settings.yaml"
    p.write_text('llm:\n  model_name: "test-model"\n', encoding="utf-8")
    return load_settings(yaml_path=p)


def _default_source() -> HuggingFacePapersSource:
    """HuggingFacePapersSource with default SourceConfig (no config block)."""
    return HuggingFacePapersSource(config=None)


def _raw_payload_from_dict(data: dict) -> RawSourcePayload:
    """Build a RawSourcePayload for normalize() tests."""
    from datetime import datetime, timezone

    return RawSourcePayload(
        source_id="huggingface_papers",
        source_type=SourceType.PAPER,
        external_id=data.get("external_id", "2401.99999"),
        raw_data=data,
        fetched_at=datetime.now(tz=timezone.utc),  # noqa: UP017
    )


# The raw_data dict produced by _entry_to_dict for the single entry fixture.
# Used as the canonical valid input for normalize() tests.
_VALID_RAW: dict = {
    "external_id": "2401.12345",
    "title": "Attention Is All You Need Again",
    "abstract": "A new transformer architecture that changes everything.",
    "authors": ["Alice Smith", "Bob Jones"],
    "published_date": "2024-01-15",
    "upvotes": 42,
}


# ---------------------------------------------------------------------------
# Class attribute tests
# ---------------------------------------------------------------------------


class TestClassAttributes:
    """Verify static class-level declarations required by SDS §5.3."""

    def test_source_id(self):
        """source_id must be 'huggingface_papers' (SDS §5.3, B3-R01)."""
        assert HuggingFacePapersSource.source_id == "huggingface_papers"

    def test_source_type(self):
        """source_type must be PAPER (SDS §5.3, B3-R02)."""
        assert HuggingFacePapersSource.source_type is SourceType.PAPER

    def test_get_config_schema_returns_source_config(self):
        """get_config_schema() must return SourceConfig (B3-R03)."""
        schema = HuggingFacePapersSource.get_config_schema()
        assert schema is SourceConfig

    def test_get_config_schema_is_classmethod(self):
        """Must be callable on the class without an instance (SDS §1.2)."""
        schema = HuggingFacePapersSource.get_config_schema()
        assert schema is SourceConfig


# ---------------------------------------------------------------------------
# Fetch tests — all HTTP is mocked with respx
# ---------------------------------------------------------------------------


class TestFetch:
    """Tests for fetch() and _fetch_with_retry() (SDS §5.3, B3-R04–R08, R17–R19).

    Uses url__startswith matching to work around respx issue #277.
    See tests/unit/sources/conftest.py for the full explanation.
    """

    def test_fetch_single_entry_returns_one_payload(self):
        """Successful fetch with one entry returns a list of length 1 (B3-R04)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=SINGLE_ENTRY_RESPONSE)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 1
        assert payloads[0].source_id == "huggingface_papers"
        assert payloads[0].source_type is SourceType.PAPER
        assert payloads[0].external_id == "2401.12345"

    def test_fetch_two_entries_returns_two_payloads(self):
        """Two entries produce two payloads in order (B3-R04)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=TWO_ENTRY_RESPONSE)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 2
        assert payloads[0].external_id == "2401.12345"
        assert payloads[1].external_id == "2401.67890"

    def test_fetch_empty_array_returns_empty_list(self):
        """Empty JSON array returns [] without error (B3-R04)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_RESPONSE)
            )
            result = _default_source().fetch()

        assert result == []

    def test_fetch_malformed_json_returns_empty_list(self):
        """Invalid JSON from the API returns [] without raising (B3-R18)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=MALFORMED_JSON)
            )
            result = _default_source().fetch()

        assert result == []

    def test_fetch_non_list_json_returns_empty_list(self):
        """A JSON object (not an array) returns [] (B3-R19)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=NON_LIST_JSON)
            )
            result = _default_source().fetch()

        assert result == []

    def test_fetch_network_timeout_returns_empty_list(self):
        """TimeoutException after tenacity exhaustion returns [] (B3-R06)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):
                result = _default_source().fetch()

        assert result == []

    def test_fetch_network_timeout_retries_three_times(self):
        """Tenacity makes 3 total attempts on TimeoutException (B3-R05, R06)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_429_rate_limit_returns_empty_list(self):
        """HTTP 429 after tenacity exhaustion returns [] (B3-R08)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                result = _default_source().fetch()

        assert result == []

    def test_fetch_429_retries_three_times(self):
        """Tenacity makes 3 total attempts on HTTP 429 (B3-R05, R08)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_401_returns_empty_list(self):
        """HTTP 401 returns [] immediately (B3-R07)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            result = _default_source().fetch()

        assert result == []

    def test_fetch_401_does_not_retry(self):
        """HTTP 401 makes exactly 1 request — no retry (B3-R07)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 1

    def test_fetch_500_retries_three_times(self):
        """HTTP 500 is retried by tenacity (transient server error) (B3-R05)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_500_returns_empty_list(self):
        """HTTP 500 after retries returns [] (B3-R04)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                result = _default_source().fetch()

        assert result == []

    def test_fetch_entry_without_id_is_skipped(self):
        """An entry with no 'id' field is skipped; others are returned (B3-R17)."""
        bad_entry = {
            "id": "",  # empty → no external_id
            "paper": {"id": "", "title": "No ID Paper", "authors": [], "upvotes": 0},
        }
        response_body = json.dumps([bad_entry, _SINGLE_ENTRY])
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=response_body)
            )
            payloads = _default_source().fetch()

        # Only the valid entry passes through
        assert len(payloads) == 1
        assert payloads[0].external_id == "2401.12345"


# ---------------------------------------------------------------------------
# Normalize tests — no HTTP involved
# ---------------------------------------------------------------------------


class TestNormalize:
    """Tests for normalize() (SDS §5.4, §4.2, B3-R09–R16)."""

    def test_normalize_returns_normalized_item(self):
        """normalize() returns a NormalizedItem for valid raw_data (B3-R09)."""
        from arip.entities import NormalizedItem

        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert isinstance(result, NormalizedItem)

    def test_normalize_source_id(self):
        """source_id is preserved from the payload (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.source_id == "huggingface_papers"

    def test_normalize_source_type_value(self):
        """source_type value is the string 'PAPER' (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.source_type == "PAPER"

    def test_normalize_external_id(self):
        """external_id is preserved from raw_data (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.external_id == "2401.12345"

    def test_normalize_title(self):
        """Title is extracted correctly (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.title == "Attention Is All You Need Again"

    def test_normalize_abstract(self):
        """Abstract is extracted correctly (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.abstract == "A new transformer architecture that changes everything."

    def test_normalize_authors(self):
        """Authors list is extracted correctly (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.authors == ["Alice Smith", "Bob Jones"]

    def test_normalize_institutions_is_none(self):
        """institutions is always None — HF API exposes no affiliations (B3-R14)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.institutions is None

    def test_normalize_primary_url(self):
        """primary_url is the HuggingFace Papers page URL (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.primary_url == "https://huggingface.co/papers/2401.12345"

    def test_normalize_additional_urls_contains_arxiv(self):
        """additional_urls contains the ArXiv abstract URL (B3-R16)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.additional_urls == ["https://arxiv.org/abs/2401.12345"]

    def test_normalize_published_date(self):
        """published_date is an ISO date string (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.published_date == "2024-01-15"

    def test_normalize_source_signals_contains_upvotes(self):
        """source_signals is {'upvotes': int} (B3-R13)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.source_signals == {"upvotes": 42}

    def test_normalize_source_signals_upvotes_value(self):
        """upvotes value in source_signals matches the raw_data value (B3-R13)."""
        data = {**_VALID_RAW, "upvotes": 99}
        payload = _raw_payload_from_dict(data)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.source_signals["upvotes"] == 99

    def test_normalize_topics_is_none(self):
        """topics is always None — daily papers endpoint has no category tags (B3-R15)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.topics is None

    def test_normalize_language_is_en(self):
        """Language defaults to EN (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.language == "EN"

    def test_normalize_content_hash_formula(self):
        """content_hash matches SDS §4.2 formula (B3-R10)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)

        expected = hashlib.sha256(
            b"huggingface_papers:2401.12345:Attention Is All You Need Again"
        ).hexdigest()
        assert result.content_hash == expected

    def test_normalize_content_hash_uses_helper(self):
        """content_hash matches compute_content_hash() from _http.py (B3-R10)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)

        expected = compute_content_hash(
            source_id="huggingface_papers",
            external_id="2401.12345",
            title="Attention Is All You Need Again",
        )
        assert result.content_hash == expected

    def test_normalize_content_hash_truncates_title_at_200(self):
        """content_hash uses only the first 200 characters of the title (SDS §4.2)."""
        long_title = "X" * 300
        data = {**_VALID_RAW, "title": long_title}
        payload = _raw_payload_from_dict(data)
        result = HuggingFacePapersSource(config=None).normalize(payload)

        expected = hashlib.sha256(
            f"huggingface_papers:2401.12345:{'X' * 200}".encode()
        ).hexdigest()
        assert result.content_hash == expected

    def test_normalize_raw_payload_is_json_of_raw_data(self):
        """raw_payload is a JSON-serialised copy of raw_data (B3-R09)."""
        payload = _raw_payload_from_dict(_VALID_RAW)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert json.loads(result.raw_payload) == _VALID_RAW

    def test_normalize_missing_title_raises_source_error(self):
        """SourceError raised when title is empty string (B3-R11)."""
        data = {**_VALID_RAW, "title": ""}
        payload = _raw_payload_from_dict(data)
        with pytest.raises(SourceError, match="title"):
            HuggingFacePapersSource(config=None).normalize(payload)

    def test_normalize_missing_title_key_raises_source_error(self):
        """SourceError raised when title key is absent from raw_data (B3-R11)."""
        data = {k: v for k, v in _VALID_RAW.items() if k != "title"}
        payload = _raw_payload_from_dict(data)
        with pytest.raises(SourceError, match="title"):
            HuggingFacePapersSource(config=None).normalize(payload)

    def test_normalize_missing_external_id_raises_source_error(self):
        """SourceError raised when external_id is empty — primary_url cannot be formed (B3-R12)."""
        data = {**_VALID_RAW, "external_id": ""}
        payload = _raw_payload_from_dict(data)
        with pytest.raises(SourceError, match="primary_url"):
            HuggingFacePapersSource(config=None).normalize(payload)

    def test_normalize_missing_external_id_key_raises_source_error(self):
        """SourceError raised when external_id key is absent from raw_data (B3-R12)."""
        data = {k: v for k, v in _VALID_RAW.items() if k != "external_id"}
        payload = _raw_payload_from_dict(data)
        with pytest.raises(SourceError, match="primary_url"):
            HuggingFacePapersSource(config=None).normalize(payload)

    def test_normalize_empty_authors_produces_none(self):
        """authors field is None when authors list is empty (B3-R09)."""
        data = {**_VALID_RAW, "authors": []}
        payload = _raw_payload_from_dict(data)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.authors is None

    def test_normalize_empty_abstract_produces_none(self):
        """abstract field is None when raw_data abstract is empty string (B3-R09)."""
        data = {**_VALID_RAW, "abstract": ""}
        payload = _raw_payload_from_dict(data)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.abstract is None

    def test_normalize_empty_published_date_produces_none(self):
        """published_date field is None when raw_data value is empty (B3-R09)."""
        data = {**_VALID_RAW, "published_date": ""}
        payload = _raw_payload_from_dict(data)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.published_date is None

    def test_normalize_zero_upvotes_stored_correctly(self):
        """upvotes=0 is stored as 0, not as None or False (B3-R13)."""
        data = {**_VALID_RAW, "upvotes": 0}
        payload = _raw_payload_from_dict(data)
        result = HuggingFacePapersSource(config=None).normalize(payload)
        assert result.source_signals == {"upvotes": 0}


# ---------------------------------------------------------------------------
# Payload structure tests — verify raw_data produced by _entry_to_dict
# ---------------------------------------------------------------------------


class TestPayloadStructure:
    """Verify the raw_data dict extracted from a real-shaped API response."""

    def _fetch_single(self) -> RawSourcePayload:
        """Return the first payload from a mocked single-entry fetch."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=SINGLE_ENTRY_RESPONSE)
            )
            return _default_source().fetch()[0]

    def test_raw_data_external_id(self):
        """raw_data['external_id'] is the top-level 'id' from the API entry."""
        assert self._fetch_single().raw_data["external_id"] == "2401.12345"

    def test_raw_data_title(self):
        """raw_data['title'] is taken from the nested paper.title field."""
        assert self._fetch_single().raw_data["title"] == "Attention Is All You Need Again"

    def test_raw_data_abstract(self):
        """raw_data['abstract'] is taken from the nested paper.summary field."""
        assert "transformer" in self._fetch_single().raw_data["abstract"]

    def test_raw_data_authors(self):
        """raw_data['authors'] is a list of author display names."""
        assert self._fetch_single().raw_data["authors"] == ["Alice Smith", "Bob Jones"]

    def test_raw_data_published_date(self):
        """raw_data['published_date'] is 'YYYY-MM-DD' truncated from ISO 8601."""
        assert self._fetch_single().raw_data["published_date"] == "2024-01-15"

    def test_raw_data_upvotes(self):
        """raw_data['upvotes'] is an integer from paper.upvotes."""
        assert self._fetch_single().raw_data["upvotes"] == 42

    def test_payload_external_id_matches_raw_data(self):
        """RawSourcePayload.external_id matches raw_data['external_id']."""
        payload = self._fetch_single()
        assert payload.external_id == payload.raw_data["external_id"]

    def test_raw_data_title_from_nested_paper_preferred(self):
        """paper.title is preferred over the top-level title field."""
        # Construct a response where top-level title differs from paper.title
        modified = {
            **_SINGLE_ENTRY,
            "title": "Top-level title (should be ignored)",
            "paper": {**_SINGLE_ENTRY["paper"], "title": "Nested paper title"},
        }
        response_body = json.dumps([modified])
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=response_body)
            )
            payload = _default_source().fetch()[0]
        assert payload.raw_data["title"] == "Nested paper title"

    def test_raw_data_id_from_top_level_preferred(self):
        """Top-level 'id' is preferred over nested paper.id for external_id."""
        modified = {
            **_SINGLE_ENTRY,
            "id": "2401.99999",
            "paper": {**_SINGLE_ENTRY["paper"], "id": "2401.00000"},
        }
        response_body = json.dumps([modified])
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=response_body)
            )
            payload = _default_source().fetch()[0]
        assert payload.raw_data["external_id"] == "2401.99999"

    def test_raw_data_id_falls_back_to_nested_paper_id(self):
        """Falls back to paper.id when top-level 'id' is absent or empty."""
        modified = {
            **_SINGLE_ENTRY,
            "id": "",  # empty top-level id
            "paper": {**_SINGLE_ENTRY["paper"], "id": "2401.11111"},
        }
        response_body = json.dumps([modified])
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=response_body)
            )
            payload = _default_source().fetch()[0]
        assert payload.raw_data["external_id"] == "2401.11111"

    def test_published_date_from_paper_publishedat_preferred(self):
        """paper.publishedAt is used for published_date when present."""
        assert self._fetch_single().raw_data["published_date"] == "2024-01-15"

    def test_authors_with_no_name_field_excluded(self):
        """Author entries without a 'name' key are silently excluded."""
        modified = {
            **_SINGLE_ENTRY,
            "paper": {
                **_SINGLE_ENTRY["paper"],
                "authors": [
                    {"name": "Valid Author"},
                    {"_id": "no_name_here"},  # no 'name' key
                ],
            },
        }
        response_body = json.dumps([modified])
        with respx.mock as mock:
            mock.route(url__startswith=HF_DAILY_PAPERS_URL).mock(
                return_value=httpx.Response(200, text=response_body)
            )
            payload = _default_source().fetch()[0]
        assert payload.raw_data["authors"] == ["Valid Author"]


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """Verify SourceRegistry discovers HuggingFacePapersSource (SDS §1.2, B3-R20, R21)."""

    def test_registry_includes_huggingface_papers(self, tmp_path: Path):
        """SourceRegistry.get_source('huggingface_papers') returns the correct instance (B3-R21)."""
        import arip.sources  # noqa: F401 — triggers __init__.py imports

        settings = _minimal_settings(tmp_path)
        registry = SourceRegistry(settings)
        source = registry.get_source("huggingface_papers")

        assert source is not None
        assert isinstance(source, HuggingFacePapersSource)

    def test_registry_includes_huggingface_papers_in_active_sources(self, tmp_path: Path):
        """HuggingFacePapersSource appears in get_active_sources() (B3-R21)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        registry = SourceRegistry(settings)
        source_ids = [s.source_id for s in registry.get_active_sources()]

        assert "huggingface_papers" in source_ids

    def test_registry_excludes_huggingface_papers_when_disabled(self, tmp_path: Path):
        """HuggingFacePapersSource is not in active sources when enabled=False (B3-R21)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        object.__setattr__(
            settings.sources, "huggingface_papers", SourceConfig(enabled=False)
        )
        registry = SourceRegistry(settings)

        assert registry.get_source("huggingface_papers") is None
        source_ids = [s.source_id for s in registry.get_active_sources()]
        assert "huggingface_papers" not in source_ids

    def test_registry_still_includes_arxiv(self, tmp_path: Path):
        """ArXivSource is still discovered after adding HuggingFacePapersSource (B3-R20)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        registry = SourceRegistry(settings)
        source_ids = [s.source_id for s in registry.get_active_sources()]

        assert "arxiv" in source_ids
        assert "huggingface_papers" in source_ids
        