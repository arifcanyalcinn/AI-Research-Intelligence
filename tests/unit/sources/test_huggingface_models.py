"""
Unit tests for HuggingFaceModelsSource.

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

Fixtures below are trimmed from real responses captured from
``https://huggingface.co/api/models?sort=downloads&direction=-1`` so that the
nested shape and tag vocabulary match production data (SDS §5.4 testing
strategy: "fixture payloads captured from real API responses").

Test coverage:
  - class attributes: source_id, source_type, get_config_schema.
  - fetch(): single entry, two entries, empty array, malformed JSON,
    non-list JSON, network timeout (retried 3×, returns []),
    HTTP 429 (retried 3×, returns []), HTTP 401 (1 attempt, returns []),
    HTTP 500 (retried 3×, returns []), entry without id skipped.
  - fetch(): request carries sort/direction/limit query parameters.
  - normalize(): title, primary_url, authors, published_date, topics,
    source_signals, content_hash, language, source_type value,
    institutions=None, abstract=None, additional_urls=None.
  - normalize(): SourceError on missing title / missing external_id.
  - normalize(): content_hash matches SDS §4.2 formula.
  - _extract_topics(): namespaced tags dropped, pipeline_tag prepended,
    duplicates removed, empty result becomes None.
  - payload structure: external_id, author fallback, downloads, likes,
    tags, pipeline_tag, library_name, published_date extraction.
  - registry integration: discovery, active sources, disabled exclusion.
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
from arip.sources.huggingface_models import (
    HF_MODELS_URL,
    HuggingFaceModelsSource,
)
from arip.sources.registry import SourceRegistry

# ---------------------------------------------------------------------------
# JSON response fixtures (trimmed from real API responses)
# ---------------------------------------------------------------------------

_SINGLE_ENTRY: dict = {
    "_id": "621ffdc136468d709f180294",
    "id": "sentence-transformers/all-MiniLM-L6-v2",
    "likes": 4975,
    "private": False,
    "downloads": 245017102,
    "tags": [
        "sentence-transformers",
        "pytorch",
        "onnx",
        "bert",
        "feature-extraction",
        "sentence-similarity",
        "transformers",
        "en",
        "dataset:s2orc",
        "arxiv:1904.06472",
        "base_model:nreimers/MiniLM-L6-H384-uncased",
        "license:apache-2.0",
        "region:us",
    ],
    "pipeline_tag": "sentence-similarity",
    "library_name": "sentence-transformers",
    "createdAt": "2022-03-02T23:29:05.000Z",
    "modelId": "sentence-transformers/all-MiniLM-L6-v2",
}

_SECOND_ENTRY: dict = {
    "_id": "621ffdc136468d709f17a20e",
    "id": "cross-encoder/ms-marco-MiniLM-L6-v2",
    "likes": 266,
    "private": False,
    "downloads": 78789614,
    "tags": [
        "sentence-transformers",
        "pytorch",
        "bert",
        "text-classification",
        "transformers",
        "text-ranking",
        "en",
        "license:apache-2.0",
        "region:us",
    ],
    "pipeline_tag": "text-ranking",
    "library_name": "sentence-transformers",
    "createdAt": "2022-03-02T23:29:05.000Z",
    "modelId": "cross-encoder/ms-marco-MiniLM-L6-v2",
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


def _default_source() -> HuggingFaceModelsSource:
    """HuggingFaceModelsSource with default SourceConfig (no config block)."""
    return HuggingFaceModelsSource(config=None)


def _raw_payload_from_dict(data: dict) -> RawSourcePayload:
    """Build a RawSourcePayload for normalize() tests."""
    from datetime import datetime, timezone

    return RawSourcePayload(
        source_id="huggingface_models",
        source_type=SourceType.MODEL,
        external_id=data.get("external_id", "org/some-model"),
        raw_data=data,
        fetched_at=datetime.now(tz=timezone.utc),  # noqa: UP017
    )


# The raw_data dict produced by _entry_to_dict for the single entry fixture.
# Used as the canonical valid input for normalize() tests.
_VALID_RAW: dict = {
    "external_id": "sentence-transformers/all-MiniLM-L6-v2",
    "title": "sentence-transformers/all-MiniLM-L6-v2",
    "author": "sentence-transformers",
    "downloads": 245017102,
    "likes": 4975,
    "tags": [
        "sentence-transformers",
        "pytorch",
        "bert",
        "sentence-similarity",
        "license:apache-2.0",
        "region:us",
    ],
    "pipeline_tag": "sentence-similarity",
    "library_name": "sentence-transformers",
    "published_date": "2022-03-02",
}


# ---------------------------------------------------------------------------
# Class attribute tests
# ---------------------------------------------------------------------------


class TestClassAttributes:
    """Verify static class-level declarations required by SDS §5.3."""

    def test_source_id(self):
        """source_id must be 'huggingface_models' (SDS §5.3, §5.15)."""
        assert HuggingFaceModelsSource.source_id == "huggingface_models"

    def test_source_type(self):
        """source_type must be MODEL (SDS §4.2 source_type column)."""
        assert HuggingFaceModelsSource.source_type is SourceType.MODEL

    def test_get_config_schema_returns_source_config(self):
        """get_config_schema() must return the plain SourceConfig (SDS §5.15)."""
        assert HuggingFaceModelsSource.get_config_schema() is SourceConfig

    def test_get_config_schema_callable_without_instance(self):
        """Must be callable on the class without an instance (SDS §1.2)."""
        assert HuggingFaceModelsSource.get_config_schema() is SourceConfig


# ---------------------------------------------------------------------------
# Fetch tests — all HTTP is mocked with respx
# ---------------------------------------------------------------------------


class TestFetch:
    """Tests for fetch() and _fetch_with_retry() (SDS §5.3).

    Uses url__startswith matching to work around respx issue #277.
    See tests/unit/sources/conftest.py for the full explanation.
    """

    def test_fetch_single_entry_returns_one_payload(self):
        """Successful fetch with one entry returns a list of length 1."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(200, text=SINGLE_ENTRY_RESPONSE)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 1
        assert payloads[0].source_id == "huggingface_models"
        assert payloads[0].source_type is SourceType.MODEL
        assert payloads[0].external_id == "sentence-transformers/all-MiniLM-L6-v2"

    def test_fetch_two_entries_returns_two_payloads_in_order(self):
        """Two entries produce two payloads in response order."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(200, text=TWO_ENTRY_RESPONSE)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 2
        assert payloads[0].external_id == "sentence-transformers/all-MiniLM-L6-v2"
        assert payloads[1].external_id == "cross-encoder/ms-marco-MiniLM-L6-v2"

    def test_fetch_empty_array_returns_empty_list(self):
        """Empty JSON array returns [] without error."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_RESPONSE)
            )
            assert _default_source().fetch() == []

    def test_fetch_malformed_json_returns_empty_list(self):
        """Invalid JSON from the API returns [] without raising."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(200, text=MALFORMED_JSON)
            )
            assert _default_source().fetch() == []

    def test_fetch_non_list_json_returns_empty_list(self):
        """A JSON object (not an array) returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(200, text=NON_LIST_JSON)
            )
            assert _default_source().fetch() == []

    def test_fetch_network_timeout_returns_empty_list(self):
        """TimeoutException after tenacity exhaustion returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):
                assert _default_source().fetch() == []

    def test_fetch_network_timeout_retries_three_times(self):
        """Tenacity makes 3 total attempts on TimeoutException."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_429_rate_limit_returns_empty_list(self):
        """HTTP 429 after tenacity exhaustion returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                assert _default_source().fetch() == []

    def test_fetch_429_retries_three_times(self):
        """Tenacity makes 3 total attempts on HTTP 429 (SDS §5.3 rate limit)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_401_returns_empty_list(self):
        """HTTP 401 returns [] immediately (SDS §5.3 auth failure)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            assert _default_source().fetch() == []

    def test_fetch_401_does_not_retry(self):
        """HTTP 401 makes exactly 1 request — credentials do not self-heal."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 1

    def test_fetch_500_retries_three_times(self):
        """HTTP 500 is retried by tenacity (transient server error)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_500_returns_empty_list(self):
        """HTTP 500 after retries returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                assert _default_source().fetch() == []

    def test_fetch_entry_without_id_is_skipped(self):
        """An entry with no usable id is skipped; valid entries still return."""
        bad_entry = {"id": "", "modelId": "", "likes": 0, "downloads": 0, "tags": []}
        body = json.dumps([bad_entry, _SINGLE_ENTRY])
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(200, text=body)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 1
        assert payloads[0].external_id == "sentence-transformers/all-MiniLM-L6-v2"

    def test_fetch_sends_sort_and_limit_query_parameters(self):
        """The request asks for the most-downloaded models with a bounded limit."""
        with respx.mock as mock:
            route = mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_RESPONSE)
            )
            _default_source().fetch()
            request_url = route.calls[0].request.url

        assert request_url.params["sort"] == "downloads"
        assert request_url.params["direction"] == "-1"
        assert int(request_url.params["limit"]) > 0


# ---------------------------------------------------------------------------
# Normalize tests — no HTTP involved
# ---------------------------------------------------------------------------


class TestNormalize:
    """Tests for normalize() (SDS §5.4, §4.2)."""

    def test_normalize_returns_normalized_item(self):
        """normalize() returns a NormalizedItem for valid raw_data."""
        from arip.entities import NormalizedItem

        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert isinstance(result, NormalizedItem)

    def test_normalize_source_id(self):
        """source_id is preserved from the payload."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.source_id == "huggingface_models"

    def test_normalize_source_type_value(self):
        """source_type value is the string 'MODEL' (SDS §4.2)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.source_type == "MODEL"

    def test_normalize_external_id(self):
        """external_id is the model repo ID."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.external_id == "sentence-transformers/all-MiniLM-L6-v2"

    def test_normalize_title_is_repo_id(self):
        """Title is the repo ID — the list endpoint has no display name."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.title == "sentence-transformers/all-MiniLM-L6-v2"

    def test_normalize_primary_url(self):
        """primary_url is the model page on the Hub (no /spaces segment)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert (
            result.primary_url
            == "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
        )

    def test_normalize_authors_is_owning_account(self):
        """authors holds the owning user/organisation."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.authors == ["sentence-transformers"]

    def test_normalize_empty_author_produces_none(self):
        """authors is None when no owning account could be determined."""
        data = {**_VALID_RAW, "author": ""}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.authors is None

    def test_normalize_institutions_is_none(self):
        """institutions is always None — the Hub exposes accounts, not affiliations."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.institutions is None

    def test_normalize_abstract_is_none(self):
        """abstract is always None — the list endpoint returns no description."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.abstract is None

    def test_normalize_additional_urls_is_none(self):
        """additional_urls is always None — no stable secondary URL exists."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.additional_urls is None

    def test_normalize_published_date(self):
        """published_date is an ISO date string derived from createdAt."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.published_date == "2022-03-02"

    def test_normalize_empty_published_date_produces_none(self):
        """published_date is None when the raw value is empty."""
        data = {**_VALID_RAW, "published_date": ""}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.published_date is None

    def test_normalize_source_signals_has_downloads_and_likes(self):
        """source_signals carries both engagement metrics (SDS §4.2, §5.5)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.source_signals == {"downloads": 245017102, "likes": 4975}

    def test_normalize_zero_signals_stored_as_zero(self):
        """Zero downloads/likes are stored as 0, not dropped or None."""
        data = {**_VALID_RAW, "downloads": 0, "likes": 0}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.source_signals == {"downloads": 0, "likes": 0}

    def test_normalize_language_is_en(self):
        """Language defaults to EN."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.language == "EN"

    def test_normalize_topics_excludes_namespaced_tags(self):
        """Namespaced tags (license:, region:) never appear in topics."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.topics is not None
        assert not any(":" in topic for topic in result.topics)

    def test_normalize_topics_includes_pipeline_tag_first(self):
        """pipeline_tag leads the topics list as the most descriptive label."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.topics[0] == "sentence-similarity"

    def test_normalize_topics_keeps_bare_tags(self):
        """Bare topical tags survive filtering."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert "pytorch" in result.topics
        assert "bert" in result.topics

    def test_normalize_topics_none_when_no_topical_tags(self):
        """topics is None when every tag is namespaced and no pipeline_tag exists."""
        data = {**_VALID_RAW, "tags": ["region:us", "license:mit"], "pipeline_tag": ""}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.topics is None

    def test_normalize_content_hash_formula(self):
        """content_hash matches the SDS §4.2 formula."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        expected = hashlib.sha256(
            b"huggingface_models:sentence-transformers/all-MiniLM-L6-v2:"
            b"sentence-transformers/all-MiniLM-L6-v2"
        ).hexdigest()
        assert result.content_hash == expected

    def test_normalize_content_hash_uses_shared_helper(self):
        """content_hash matches compute_content_hash() from _http.py (no duplication)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        expected = compute_content_hash(
            source_id="huggingface_models",
            external_id="sentence-transformers/all-MiniLM-L6-v2",
            title="sentence-transformers/all-MiniLM-L6-v2",
        )
        assert result.content_hash == expected

    def test_normalize_content_hash_truncates_title_at_200(self):
        """content_hash uses only the first 200 characters of the title (SDS §4.2)."""
        long_title = "X" * 300
        data = {**_VALID_RAW, "title": long_title, "external_id": "org/m"}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        expected = hashlib.sha256(
            f"huggingface_models:org/m:{'X' * 200}".encode()
        ).hexdigest()
        assert result.content_hash == expected

    def test_normalize_raw_payload_is_json_of_raw_data(self):
        """raw_payload is a JSON-serialised copy of raw_data (SDS §4.2)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert json.loads(result.raw_payload) == _VALID_RAW

    def test_normalize_missing_title_raises_source_error(self):
        """SourceError raised when title is an empty string (SDS §5.4)."""
        data = {**_VALID_RAW, "title": ""}
        with pytest.raises(SourceError, match="title"):
            _default_source().normalize(_raw_payload_from_dict(data))

    def test_normalize_missing_title_key_raises_source_error(self):
        """SourceError raised when the title key is absent."""
        data = {k: v for k, v in _VALID_RAW.items() if k != "title"}
        with pytest.raises(SourceError, match="title"):
            _default_source().normalize(_raw_payload_from_dict(data))

    def test_normalize_missing_external_id_raises_source_error(self):
        """SourceError raised when external_id is empty — primary_url is unformable."""
        data = {**_VALID_RAW, "external_id": ""}
        with pytest.raises(SourceError, match="primary_url"):
            _default_source().normalize(_raw_payload_from_dict(data))

    def test_normalize_missing_external_id_key_raises_source_error(self):
        """SourceError raised when the external_id key is absent."""
        data = {k: v for k, v in _VALID_RAW.items() if k != "external_id"}
        with pytest.raises(SourceError, match="primary_url"):
            _default_source().normalize(_raw_payload_from_dict(data))


# ---------------------------------------------------------------------------
# Topic extraction — behaviour of the tag filter
# ---------------------------------------------------------------------------


class TestTopicExtraction:
    """Verify the tag → topic selection rule used by the ranking topic signal."""

    def test_namespaced_tags_are_dropped(self):
        """Tags of the form key:value are treated as metadata, not topics."""
        topics = HuggingFaceModelsSource._extract_topics(
            tags=["bert", "license:mit", "dataset:squad", "arxiv:1234.5678"],
            pipeline_tag="",
        )
        assert topics == ["bert"]

    def test_pipeline_tag_is_prepended(self):
        """pipeline_tag leads the list when not already present."""
        topics = HuggingFaceModelsSource._extract_topics(
            tags=["pytorch"], pipeline_tag="text-generation"
        )
        assert topics == ["text-generation", "pytorch"]

    def test_pipeline_tag_not_duplicated(self):
        """pipeline_tag appearing in tags is not repeated."""
        topics = HuggingFaceModelsSource._extract_topics(
            tags=["text-generation", "pytorch"], pipeline_tag="text-generation"
        )
        assert topics == ["text-generation", "pytorch"]

    def test_duplicate_tags_removed(self):
        """Repeated tags appear once."""
        topics = HuggingFaceModelsSource._extract_topics(
            tags=["bert", "bert", "pytorch"], pipeline_tag=""
        )
        assert topics == ["bert", "pytorch"]

    def test_empty_input_returns_empty_list(self):
        """No tags and no pipeline_tag yields an empty list."""
        assert HuggingFaceModelsSource._extract_topics(tags=[], pipeline_tag="") == []


# ---------------------------------------------------------------------------
# Payload structure tests — verify raw_data produced by _entry_to_dict
# ---------------------------------------------------------------------------


class TestPayloadStructure:
    """Verify the raw_data dict extracted from a real-shaped API response."""

    def _fetch_single(self, body: str = SINGLE_ENTRY_RESPONSE) -> RawSourcePayload:
        """Return the first payload from a mocked single-entry fetch."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_MODELS_URL).mock(
                return_value=httpx.Response(200, text=body)
            )
            return _default_source().fetch()[0]

    def test_raw_data_external_id(self):
        """raw_data['external_id'] is the top-level 'id'."""
        assert (
            self._fetch_single().raw_data["external_id"]
            == "sentence-transformers/all-MiniLM-L6-v2"
        )

    def test_raw_data_title_equals_external_id(self):
        """raw_data['title'] mirrors the repo ID."""
        raw = self._fetch_single().raw_data
        assert raw["title"] == raw["external_id"]

    def test_raw_data_author_derived_from_repo_id(self):
        """author falls back to the org prefix when the API omits the field."""
        assert self._fetch_single().raw_data["author"] == "sentence-transformers"

    def test_raw_data_author_prefers_explicit_field(self):
        """An explicit author field wins over the repo ID prefix."""
        modified = {**_SINGLE_ENTRY, "author": "explicit-owner"}
        raw = self._fetch_single(json.dumps([modified])).raw_data
        assert raw["author"] == "explicit-owner"

    def test_raw_data_author_empty_when_no_org_prefix(self):
        """A repo ID without a '/' yields no author."""
        modified = {**_SINGLE_ENTRY, "id": "gpt2", "modelId": "gpt2"}
        raw = self._fetch_single(json.dumps([modified])).raw_data
        assert raw["author"] == ""

    def test_raw_data_downloads(self):
        """raw_data['downloads'] is an integer."""
        assert self._fetch_single().raw_data["downloads"] == 245017102

    def test_raw_data_likes(self):
        """raw_data['likes'] is an integer."""
        assert self._fetch_single().raw_data["likes"] == 4975

    def test_raw_data_pipeline_tag(self):
        """raw_data['pipeline_tag'] is the task category."""
        assert self._fetch_single().raw_data["pipeline_tag"] == "sentence-similarity"

    def test_raw_data_library_name(self):
        """raw_data['library_name'] is the framework."""
        assert self._fetch_single().raw_data["library_name"] == "sentence-transformers"

    def test_raw_data_tags_unfiltered(self):
        """raw_data['tags'] preserves the raw tag list including namespaced ones."""
        tags = self._fetch_single().raw_data["tags"]
        assert "license:apache-2.0" in tags
        assert "bert" in tags

    def test_raw_data_published_date_truncated(self):
        """createdAt is truncated from ISO 8601 to YYYY-MM-DD."""
        assert self._fetch_single().raw_data["published_date"] == "2022-03-02"

    def test_raw_data_id_falls_back_to_model_id(self):
        """external_id falls back to modelId when top-level 'id' is empty."""
        modified = {**_SINGLE_ENTRY, "id": "", "modelId": "fallback/model"}
        raw = self._fetch_single(json.dumps([modified])).raw_data
        assert raw["external_id"] == "fallback/model"

    def test_raw_data_missing_counts_default_to_zero(self):
        """Absent downloads/likes become 0 rather than raising."""
        modified = {k: v for k, v in _SINGLE_ENTRY.items()
                    if k not in ("downloads", "likes")}
        raw = self._fetch_single(json.dumps([modified])).raw_data
        assert raw["downloads"] == 0
        assert raw["likes"] == 0

    def test_raw_data_null_counts_default_to_zero(self):
        """Explicit nulls for downloads/likes become 0."""
        modified = {**_SINGLE_ENTRY, "downloads": None, "likes": None}
        raw = self._fetch_single(json.dumps([modified])).raw_data
        assert raw["downloads"] == 0
        assert raw["likes"] == 0

    def test_payload_external_id_matches_raw_data(self):
        """RawSourcePayload.external_id matches raw_data['external_id']."""
        payload = self._fetch_single()
        assert payload.external_id == payload.raw_data["external_id"]

    def test_fetch_then_normalize_round_trip(self):
        """A payload straight from fetch() normalises without error."""
        payload = self._fetch_single()
        result = _default_source().normalize(payload)
        assert result.external_id == "sentence-transformers/all-MiniLM-L6-v2"
        assert result.source_signals["downloads"] == 245017102


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """Verify SourceRegistry discovers HuggingFaceModelsSource (SDS §1.2)."""

    def test_registry_includes_huggingface_models(self, tmp_path: Path):
        """get_source('huggingface_models') returns the correct instance."""
        import arip.sources  # noqa: F401 — triggers __init__.py imports

        registry = SourceRegistry(_minimal_settings(tmp_path))
        source = registry.get_source("huggingface_models")

        assert source is not None
        assert isinstance(source, HuggingFaceModelsSource)

    def test_registry_includes_huggingface_models_in_active_sources(self, tmp_path: Path):
        """HuggingFaceModelsSource appears in get_active_sources()."""
        import arip.sources  # noqa: F401

        registry = SourceRegistry(_minimal_settings(tmp_path))
        source_ids = [s.source_id for s in registry.get_active_sources()]

        assert "huggingface_models" in source_ids

    def test_registry_excludes_huggingface_models_when_disabled(self, tmp_path: Path):
        """Not in active sources when enabled=False (SDS §5.2)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        object.__setattr__(
            settings.sources, "huggingface_models", SourceConfig(enabled=False)
        )
        registry = SourceRegistry(settings)

        assert registry.get_source("huggingface_models") is None
        source_ids = [s.source_id for s in registry.get_active_sources()]
        assert "huggingface_models" not in source_ids

    def test_registry_still_includes_previous_batch_sources(self, tmp_path: Path):
        """Batch 2 and 3 sources are unaffected by this batch's registration."""
        import arip.sources  # noqa: F401

        registry = SourceRegistry(_minimal_settings(tmp_path))
        source_ids = [s.source_id for s in registry.get_active_sources()]

        assert "arxiv" in source_ids
        assert "huggingface_papers" in source_ids
        assert "huggingface_models" in source_ids

    def test_source_is_direct_subclass_of_base_source(self):
        """Discovery requires a *direct* BaseSource subclass.

        SourceRegistry scans ``BaseSource.__subclasses__()``, which does not
        recurse.  An intermediate base class shared with the Spaces source
        would hide both plugins from discovery, so this guards that decision.
        """
        from arip.interfaces import BaseSource

        assert HuggingFaceModelsSource in BaseSource.__subclasses__()
        