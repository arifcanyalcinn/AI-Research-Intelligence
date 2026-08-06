"""
Unit tests for HuggingFaceSpacesSource.

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
``https://huggingface.co/api/spaces?sort=likes&direction=-1`` so that the shape
and tag vocabulary match production data (SDS §5.4 testing strategy: "fixture
payloads captured from real API responses").

Test coverage:
  - class attributes: source_id, source_type, get_config_schema.
  - fetch(): single entry, two entries, empty array, malformed JSON,
    non-list JSON, network timeout (retried 3×, returns []),
    HTTP 429 (retried 3×, returns []), HTTP 401 (1 attempt, returns []),
    HTTP 500 (retried 3×, returns []), entry without id skipped.
  - fetch(): request carries sort/direction/limit query parameters.
  - normalize(): title, primary_url (/spaces segment), authors, published_date,
    topics, source_signals, content_hash, language, source_type value,
    institutions=None, abstract=None, additional_urls=None.
  - normalize(): source_signals has no 'downloads' key (Spaces report none).
  - normalize(): SourceError on missing title / missing external_id.
  - _extract_topics(): namespaced tags dropped, sdk prepended, dedup.
  - payload structure: cardData.title preference, author fallback, likes, sdk.
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
from arip.sources.huggingface_spaces import (
    HF_SPACES_URL,
    HuggingFaceSpacesSource,
)
from arip.sources.registry import SourceRegistry

# ---------------------------------------------------------------------------
# JSON response fixtures (trimmed from real API responses)
# ---------------------------------------------------------------------------

_SINGLE_ENTRY: dict = {
    "_id": "67e454cdd4f34388bcd19c38",
    "id": "enzostvs/deepsite",
    "likes": 16617,
    "private": False,
    "sdk": "docker",
    "tags": ["docker", "region:us"],
    "createdAt": "2025-03-26T19:26:05.000Z",
}

_SECOND_ENTRY: dict = {
    "_id": "643d3016d2c1e08a5eca0c22",
    "id": "open-llm-leaderboard/open_llm_leaderboard",
    "likes": 14059,
    "private": False,
    "sdk": "docker",
    "tags": [
        "docker",
        "leaderboard",
        "modality:text",
        "submission:automatic",
        "language:english",
        "region:us",
    ],
    "createdAt": "2023-04-17T11:40:06.000Z",
}

# A richer entry as returned when the API includes cardData.
_CARD_DATA_ENTRY: dict = {
    "_id": "6a5ccd9920d4f7e0a3f4a01a",
    "id": "cinderholm/wan2-2-i2v-v3",
    "author": "cinderholm",
    "cardData": {
        "title": "Wan2.2 14B Fast Preview",
        "emoji": "🏆",
        "sdk": "gradio",
        "app_file": "app.py",
        "pinned": False,
    },
    "likes": 561,
    "trendingScore": 275,
    "private": False,
    "sdk": "gradio",
    "tags": ["gradio", "mcp-server", "region:us"],
    "createdAt": "2026-07-19T13:14:01.000Z",
}

SINGLE_ENTRY_RESPONSE: str = json.dumps([_SINGLE_ENTRY])
TWO_ENTRY_RESPONSE: str = json.dumps([_SINGLE_ENTRY, _SECOND_ENTRY])
CARD_DATA_RESPONSE: str = json.dumps([_CARD_DATA_ENTRY])
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


def _default_source() -> HuggingFaceSpacesSource:
    """HuggingFaceSpacesSource with default SourceConfig (no config block)."""
    return HuggingFaceSpacesSource(config=None)


def _raw_payload_from_dict(data: dict) -> RawSourcePayload:
    """Build a RawSourcePayload for normalize() tests."""
    from datetime import datetime, timezone

    return RawSourcePayload(
        source_id="huggingface_spaces",
        source_type=SourceType.SPACE,
        external_id=data.get("external_id", "org/some-space"),
        raw_data=data,
        fetched_at=datetime.now(tz=timezone.utc),  # noqa: UP017
    )


# The raw_data dict produced by _entry_to_dict for the single entry fixture.
_VALID_RAW: dict = {
    "external_id": "enzostvs/deepsite",
    "title": "enzostvs/deepsite",
    "author": "enzostvs",
    "likes": 16617,
    "sdk": "docker",
    "tags": ["docker", "region:us"],
    "published_date": "2025-03-26",
}


# ---------------------------------------------------------------------------
# Class attribute tests
# ---------------------------------------------------------------------------


class TestClassAttributes:
    """Verify static class-level declarations required by SDS §5.3."""

    def test_source_id(self):
        """source_id must be 'huggingface_spaces' (SDS §5.3, §5.15)."""
        assert HuggingFaceSpacesSource.source_id == "huggingface_spaces"

    def test_source_type(self):
        """source_type must be SPACE (SDS §4.2 source_type column)."""
        assert HuggingFaceSpacesSource.source_type is SourceType.SPACE

    def test_get_config_schema_returns_source_config(self):
        """get_config_schema() must return the plain SourceConfig (SDS §5.15)."""
        assert HuggingFaceSpacesSource.get_config_schema() is SourceConfig

    def test_get_config_schema_callable_without_instance(self):
        """Must be callable on the class without an instance (SDS §1.2)."""
        assert HuggingFaceSpacesSource.get_config_schema() is SourceConfig


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
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(200, text=SINGLE_ENTRY_RESPONSE)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 1
        assert payloads[0].source_id == "huggingface_spaces"
        assert payloads[0].source_type is SourceType.SPACE
        assert payloads[0].external_id == "enzostvs/deepsite"

    def test_fetch_two_entries_returns_two_payloads_in_order(self):
        """Two entries produce two payloads in response order."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(200, text=TWO_ENTRY_RESPONSE)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 2
        assert payloads[0].external_id == "enzostvs/deepsite"
        assert payloads[1].external_id == "open-llm-leaderboard/open_llm_leaderboard"

    def test_fetch_empty_array_returns_empty_list(self):
        """Empty JSON array returns [] without error."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_RESPONSE)
            )
            assert _default_source().fetch() == []

    def test_fetch_malformed_json_returns_empty_list(self):
        """Invalid JSON from the API returns [] without raising."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(200, text=MALFORMED_JSON)
            )
            assert _default_source().fetch() == []

    def test_fetch_non_list_json_returns_empty_list(self):
        """A JSON object (not an array) returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(200, text=NON_LIST_JSON)
            )
            assert _default_source().fetch() == []

    def test_fetch_network_timeout_returns_empty_list(self):
        """TimeoutException after tenacity exhaustion returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):
                assert _default_source().fetch() == []

    def test_fetch_network_timeout_retries_three_times(self):
        """Tenacity makes 3 total attempts on TimeoutException."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_429_rate_limit_returns_empty_list(self):
        """HTTP 429 after tenacity exhaustion returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                assert _default_source().fetch() == []

    def test_fetch_429_retries_three_times(self):
        """Tenacity makes 3 total attempts on HTTP 429 (SDS §5.3 rate limit)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_401_returns_empty_list(self):
        """HTTP 401 returns [] immediately (SDS §5.3 auth failure)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            assert _default_source().fetch() == []

    def test_fetch_401_does_not_retry(self):
        """HTTP 401 makes exactly 1 request — credentials do not self-heal."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 1

    def test_fetch_500_retries_three_times(self):
        """HTTP 500 is retried by tenacity (transient server error)."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_500_returns_empty_list(self):
        """HTTP 500 after retries returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                assert _default_source().fetch() == []

    def test_fetch_entry_without_id_is_skipped(self):
        """An entry with no usable id is skipped; valid entries still return."""
        bad_entry = {"id": "", "likes": 0, "tags": []}
        body = json.dumps([bad_entry, _SINGLE_ENTRY])
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(200, text=body)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 1
        assert payloads[0].external_id == "enzostvs/deepsite"

    def test_fetch_sends_sort_and_limit_query_parameters(self):
        """The request asks for the most-liked Spaces with a bounded limit."""
        with respx.mock as mock:
            route = mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_RESPONSE)
            )
            _default_source().fetch()
            request_url = route.calls[0].request.url

        assert request_url.params["sort"] == "likes"
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
        assert result.source_id == "huggingface_spaces"

    def test_normalize_source_type_value(self):
        """source_type value is the string 'SPACE' (SDS §4.2)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.source_type == "SPACE"

    def test_normalize_external_id(self):
        """external_id is the Space repo ID."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.external_id == "enzostvs/deepsite"

    def test_normalize_primary_url_includes_spaces_segment(self):
        """primary_url has the /spaces path segment that model URLs lack."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.primary_url == "https://huggingface.co/spaces/enzostvs/deepsite"

    def test_normalize_authors_is_owning_account(self):
        """authors holds the owning user/organisation."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.authors == ["enzostvs"]

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
        assert result.published_date == "2025-03-26"

    def test_normalize_empty_published_date_produces_none(self):
        """published_date is None when the raw value is empty."""
        data = {**_VALID_RAW, "published_date": ""}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.published_date is None

    def test_normalize_source_signals_has_likes(self):
        """source_signals carries the likes count (SDS §4.2, §5.5)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.source_signals == {"likes": 16617}

    def test_normalize_source_signals_has_no_downloads_key(self):
        """No 'downloads' key — the Spaces API reports no download metric.

        Emitting a hardcoded zero would misrepresent a missing metric as a
        measured value of zero to the engagement signal in ranking.
        """
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert "downloads" not in result.source_signals

    def test_normalize_zero_likes_stored_as_zero(self):
        """Zero likes is stored as 0, not dropped or None."""
        data = {**_VALID_RAW, "likes": 0}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.source_signals == {"likes": 0}

    def test_normalize_language_is_en(self):
        """Language defaults to EN."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.language == "EN"

    def test_normalize_topics_excludes_namespaced_tags(self):
        """Namespaced tags (region:, modality:) never appear in topics."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.topics is not None
        assert not any(":" in topic for topic in result.topics)

    def test_normalize_topics_includes_sdk(self):
        """The runtime SDK appears in topics."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert "docker" in result.topics

    def test_normalize_topics_none_when_no_topical_tags(self):
        """topics is None when every tag is namespaced and no sdk exists."""
        data = {**_VALID_RAW, "tags": ["region:us"], "sdk": ""}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.topics is None

    def test_normalize_content_hash_formula(self):
        """content_hash matches the SDS §4.2 formula."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        expected = hashlib.sha256(
            b"huggingface_spaces:enzostvs/deepsite:enzostvs/deepsite"
        ).hexdigest()
        assert result.content_hash == expected

    def test_normalize_content_hash_uses_shared_helper(self):
        """content_hash matches compute_content_hash() from _http.py (no duplication)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        expected = compute_content_hash(
            source_id="huggingface_spaces",
            external_id="enzostvs/deepsite",
            title="enzostvs/deepsite",
        )
        assert result.content_hash == expected

    def test_normalize_content_hash_truncates_title_at_200(self):
        """content_hash uses only the first 200 characters of the title (SDS §4.2)."""
        data = {**_VALID_RAW, "title": "X" * 300, "external_id": "org/s"}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        expected = hashlib.sha256(
            f"huggingface_spaces:org/s:{'X' * 200}".encode()
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
        topics = HuggingFaceSpacesSource._extract_topics(
            tags=["leaderboard", "region:us", "modality:text"], sdk=""
        )
        assert topics == ["leaderboard"]

    def test_sdk_is_prepended(self):
        """The SDK leads the list when not already present."""
        topics = HuggingFaceSpacesSource._extract_topics(
            tags=["mcp-server"], sdk="gradio"
        )
        assert topics == ["gradio", "mcp-server"]

    def test_sdk_not_duplicated(self):
        """An SDK already present in tags is not repeated."""
        topics = HuggingFaceSpacesSource._extract_topics(
            tags=["gradio", "mcp-server"], sdk="gradio"
        )
        assert topics == ["gradio", "mcp-server"]

    def test_duplicate_tags_removed(self):
        """Repeated tags appear once."""
        topics = HuggingFaceSpacesSource._extract_topics(
            tags=["docker", "docker", "leaderboard"], sdk=""
        )
        assert topics == ["docker", "leaderboard"]

    def test_empty_input_returns_empty_list(self):
        """No tags and no sdk yields an empty list."""
        assert HuggingFaceSpacesSource._extract_topics(tags=[], sdk="") == []


# ---------------------------------------------------------------------------
# Payload structure tests — verify raw_data produced by _entry_to_dict
# ---------------------------------------------------------------------------


class TestPayloadStructure:
    """Verify the raw_data dict extracted from a real-shaped API response."""

    def _fetch_single(self, body: str = SINGLE_ENTRY_RESPONSE) -> RawSourcePayload:
        """Return the first payload from a mocked single-entry fetch."""
        with respx.mock as mock:
            mock.route(url__startswith=HF_SPACES_URL).mock(
                return_value=httpx.Response(200, text=body)
            )
            return _default_source().fetch()[0]

    def test_raw_data_external_id(self):
        """raw_data['external_id'] is the top-level 'id'."""
        assert self._fetch_single().raw_data["external_id"] == "enzostvs/deepsite"

    def test_raw_data_title_falls_back_to_repo_id(self):
        """title falls back to the repo ID when cardData is absent."""
        assert self._fetch_single().raw_data["title"] == "enzostvs/deepsite"

    def test_raw_data_title_prefers_card_data_title(self):
        """A human-readable cardData.title wins over the repo ID."""
        raw = self._fetch_single(CARD_DATA_RESPONSE).raw_data
        assert raw["title"] == "Wan2.2 14B Fast Preview"

    def test_raw_data_title_ignores_non_dict_card_data(self):
        """A malformed cardData does not break title resolution."""
        modified = {**_SINGLE_ENTRY, "cardData": "not-a-dict"}
        raw = self._fetch_single(json.dumps([modified])).raw_data
        assert raw["title"] == "enzostvs/deepsite"

    def test_raw_data_author_derived_from_repo_id(self):
        """author falls back to the org prefix when the API omits the field."""
        assert self._fetch_single().raw_data["author"] == "enzostvs"

    def test_raw_data_author_prefers_explicit_field(self):
        """An explicit author field is used when present."""
        raw = self._fetch_single(CARD_DATA_RESPONSE).raw_data
        assert raw["author"] == "cinderholm"

    def test_raw_data_likes(self):
        """raw_data['likes'] is an integer."""
        assert self._fetch_single().raw_data["likes"] == 16617

    def test_raw_data_sdk(self):
        """raw_data['sdk'] is the runtime SDK."""
        assert self._fetch_single().raw_data["sdk"] == "docker"

    def test_raw_data_tags_unfiltered(self):
        """raw_data['tags'] preserves the raw tag list including namespaced ones."""
        tags = self._fetch_single().raw_data["tags"]
        assert "region:us" in tags
        assert "docker" in tags

    def test_raw_data_published_date_truncated(self):
        """createdAt is truncated from ISO 8601 to YYYY-MM-DD."""
        assert self._fetch_single().raw_data["published_date"] == "2025-03-26"

    def test_raw_data_missing_likes_defaults_to_zero(self):
        """An absent likes field becomes 0 rather than raising."""
        modified = {k: v for k, v in _SINGLE_ENTRY.items() if k != "likes"}
        assert self._fetch_single(json.dumps([modified])).raw_data["likes"] == 0

    def test_raw_data_null_likes_defaults_to_zero(self):
        """An explicit null likes becomes 0."""
        modified = {**_SINGLE_ENTRY, "likes": None}
        assert self._fetch_single(json.dumps([modified])).raw_data["likes"] == 0

    def test_payload_external_id_matches_raw_data(self):
        """RawSourcePayload.external_id matches raw_data['external_id']."""
        payload = self._fetch_single()
        assert payload.external_id == payload.raw_data["external_id"]

    def test_fetch_then_normalize_round_trip(self):
        """A payload straight from fetch() normalises without error."""
        payload = self._fetch_single()
        result = _default_source().normalize(payload)
        assert result.external_id == "enzostvs/deepsite"
        assert result.source_signals["likes"] == 16617

    def test_card_data_entry_round_trip_uses_card_title_in_url_and_hash(self):
        """A cardData title becomes the item title while the URL keeps the repo ID."""
        payload = self._fetch_single(CARD_DATA_RESPONSE)
        result = _default_source().normalize(payload)
        assert result.title == "Wan2.2 14B Fast Preview"
        assert (
            result.primary_url
            == "https://huggingface.co/spaces/cinderholm/wan2-2-i2v-v3"
        )


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """Verify SourceRegistry discovers HuggingFaceSpacesSource (SDS §1.2)."""

    def test_registry_includes_huggingface_spaces(self, tmp_path: Path):
        """get_source('huggingface_spaces') returns the correct instance."""
        import arip.sources  # noqa: F401 — triggers __init__.py imports

        registry = SourceRegistry(_minimal_settings(tmp_path))
        source = registry.get_source("huggingface_spaces")

        assert source is not None
        assert isinstance(source, HuggingFaceSpacesSource)

    def test_registry_includes_huggingface_spaces_in_active_sources(self, tmp_path: Path):
        """HuggingFaceSpacesSource appears in get_active_sources()."""
        import arip.sources  # noqa: F401

        registry = SourceRegistry(_minimal_settings(tmp_path))
        source_ids = [s.source_id for s in registry.get_active_sources()]

        assert "huggingface_spaces" in source_ids

    def test_registry_excludes_huggingface_spaces_when_disabled(self, tmp_path: Path):
        """Not in active sources when enabled=False (SDS §5.2)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        object.__setattr__(
            settings.sources, "huggingface_spaces", SourceConfig(enabled=False)
        )
        registry = SourceRegistry(settings)

        assert registry.get_source("huggingface_spaces") is None
        source_ids = [s.source_id for s in registry.get_active_sources()]
        assert "huggingface_spaces" not in source_ids

    def test_disabling_spaces_leaves_models_active(self, tmp_path: Path):
        """The two Batch 4 sources are independently toggleable (SDS §5.2)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        object.__setattr__(
            settings.sources, "huggingface_spaces", SourceConfig(enabled=False)
        )
        registry = SourceRegistry(settings)
        source_ids = [s.source_id for s in registry.get_active_sources()]

        assert "huggingface_spaces" not in source_ids
        assert "huggingface_models" in source_ids

    def test_all_four_sources_registered(self, tmp_path: Path):
        """Batches 2–4 all present after this batch's registration."""
        import arip.sources  # noqa: F401

        registry = SourceRegistry(_minimal_settings(tmp_path))
        source_ids = [s.source_id for s in registry.get_active_sources()]

        for expected in (
            "arxiv",
            "huggingface_papers",
            "huggingface_models",
            "huggingface_spaces",
        ):
            assert expected in source_ids

    def test_source_is_direct_subclass_of_base_source(self):
        """Discovery requires a *direct* BaseSource subclass.

        SourceRegistry scans ``BaseSource.__subclasses__()``, which does not
        recurse.  An intermediate base class shared with the Models source
        would hide both plugins from discovery, so this guards that decision.
        """
        from arip.interfaces import BaseSource

        assert HuggingFaceSpacesSource in BaseSource.__subclasses__()
        