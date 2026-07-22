"""
Unit tests for ArXivSource.

SDS §5.3 testing strategy for sources:
  "Mock HTTP responses using respx (for httpx).
   Test: successful fetch, network timeout, 429 rate limit, malformed response.
   No network calls in tests."

Every test in this file is strictly offline — all HTTP is intercepted by respx
at the httpx transport layer.  respx raises ``RuntimeError`` if any unmocked
request is attempted.

Test coverage:
  - Successful fetch: correct number of payloads, correct field values.
  - Empty feed: fetch returns [].
  - Network timeout: fetch returns [] after tenacity exhaustion.
  - HTTP 429 rate limit: fetch returns [] after tenacity exhaustion.
  - HTTP 401 auth failure: fetch returns [] with exactly 1 HTTP attempt.
  - Malformed XML: fetch returns [].
  - normalize(): all canonical fields extracted from valid raw_data.
  - normalize(): SourceError raised when title is missing.
  - normalize(): SourceError raised when primary_url is missing.
  - normalize(): content_hash matches SDS §4.2 formula.
  - normalize(): ArXiv ID version suffix is stripped ("2401.12345v1" → "2401.12345").
  - normalize(): institutions extracted from arxiv:affiliation elements.
  - normalize(): PDF URL stored in additional_urls when present.
  - normalize(): source_signals is None (no engagement metrics from ArXiv API).
  - source_id and source_type class attributes are correct.
  - get_config_schema() returns ArxivSourceConfig.
  - SourceRegistry discovers ArXivSource after sources package is imported.
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from arip.config import ArxivSourceConfig, load_settings
from arip.entities import RawSourcePayload
from arip.enums import SourceType
from arip.exceptions import SourceError
from arip.sources._http import compute_content_hash
from arip.sources.arxiv import ARXIV_BASE_URL, ArXivSource
from arip.sources.registry import SourceRegistry

# ---------------------------------------------------------------------------
# Atom XML fixtures
# ---------------------------------------------------------------------------

SINGLE_ENTRY_FEED = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2401.12345v1</id>
        <updated>2024-01-15T20:00:00-05:00</updated>
        <published>2024-01-15T20:00:00-05:00</published>
        <title>Attention Is All You Need Again</title>
        <summary>A new transformer architecture that changes everything.</summary>
        <author>
          <name>Alice Smith</name>
          <arxiv:affiliation>MIT</arxiv:affiliation>
        </author>
        <author>
          <name>Bob Jones</name>
          <arxiv:affiliation>Stanford University</arxiv:affiliation>
        </author>
        <link href="http://arxiv.org/abs/2401.12345v1" rel="alternate" type="text/html"/>
        <link title="pdf" href="http://arxiv.org/pdf/2401.12345v1" rel="related"
              type="application/pdf"/>
        <arxiv:primary_category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
        <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
        <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
      </entry>
    </feed>
    """
)

TWO_ENTRY_FEED = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2401.11111v2</id>
        <published>2024-01-14T10:00:00-05:00</published>
        <title>Paper One</title>
        <summary>Abstract one.</summary>
        <author><name>Carol White</name></author>
        <link href="http://arxiv.org/abs/2401.11111v2" rel="alternate" type="text/html"/>
        <arxiv:primary_category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
        <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
      </entry>
      <entry>
        <id>http://arxiv.org/abs/2401.22222v1</id>
        <published>2024-01-13T08:00:00-05:00</published>
        <title>Paper Two</title>
        <summary>Abstract two.</summary>
        <author><name>Dave Green</name></author>
        <link href="http://arxiv.org/abs/2401.22222v1" rel="alternate" type="text/html"/>
        <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
        <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
      </entry>
    </feed>
    """
)

EMPTY_FEED = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
    </feed>
    """
)

MALFORMED_XML = "this is not xml at all <<<"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_settings(tmp_path: Path):
    """Return an AppSettings with only the required llm.model_name field."""
    p = tmp_path / "settings.yaml"
    p.write_text('llm:\n  model_name: "test-model"\n', encoding="utf-8")
    return load_settings(yaml_path=p)


def _default_source() -> ArXivSource:
    """ArXivSource with default ArxivSourceConfig (no config block)."""
    return ArXivSource(config=None)


def _source_with_config(**kwargs) -> ArXivSource:
    """ArXivSource with a custom ArxivSourceConfig."""
    return ArXivSource(config=ArxivSourceConfig(**kwargs))


def _raw_payload_from_dict(data: dict) -> RawSourcePayload:
    """Build a minimal RawSourcePayload for normalize() tests."""
    from datetime import datetime, timezone

    return RawSourcePayload(
        source_id="arxiv",
        source_type=SourceType.PAPER,
        external_id=data.get("external_id", "2401.99999"),
        raw_data=data,
        fetched_at=datetime.now(tz=timezone.utc),  # noqa: UP017
    )


# ---------------------------------------------------------------------------
# Class attribute tests
# ---------------------------------------------------------------------------


class TestClassAttributes:
    """Verify static class-level declarations required by SDS §5.3."""

    def test_source_id(self):
        """source_id must be 'arxiv' (SDS §5.3, B2-R01)."""
        assert ArXivSource.source_id == "arxiv"

    def test_source_type(self):
        """source_type must be PAPER (SDS §5.3, B2-R02)."""
        assert ArXivSource.source_type is SourceType.PAPER

    def test_get_config_schema_returns_arxiv_config(self):
        """get_config_schema() must be a classmethod returning ArxivSourceConfig (B2-R03)."""
        schema = ArXivSource.get_config_schema()
        assert schema is ArxivSourceConfig

    def test_get_config_schema_is_classmethod(self):
        """Must be callable on the class without an instance (SDS §1.2 classmethod fix)."""
        # Calling on the class directly, no instance required.
        schema = ArXivSource.get_config_schema()
        assert schema is ArxivSourceConfig


# ---------------------------------------------------------------------------
# Fetch tests — all HTTP is mocked with respx
# ---------------------------------------------------------------------------


class TestFetch:
    """Tests for fetch() and _fetch_with_retry() (SDS §5.3 B2-R04, R05, R06, R07, R08).

    WORKAROUND — respx issue #277 (fixed in respx ≥ 0.22.0, pinned to 0.21.1):
    ``HTTPCoreMocker.to_httpx_request`` passes the httpcore method as bytes
    (``b'GET'``) directly to ``httpx.Request``.  ``Method.parse()`` therefore
    returns bytes while ``Method._eq()`` compares against the stored str
    ``'GET'``, making the equality check always False.  As a result
    ``mock.get(URL)`` — which registers a ``Method('GET')`` pattern — never
    matches and every request raises ``AllMockedAssertionError``.

    Fix: use ``mock.route(url__startswith=URL)`` which registers only a URL
    pattern (no Method pattern) and correctly matches GET requests to the
    ArXiv endpoint regardless of query parameters appended by the client.

    If respx is upgraded to ≥ 0.22.0 this workaround is no longer needed and
    ``mock.get(URL)`` can be restored.  See:
      https://github.com/lundberg/respx/issues/277
      https://github.com/lundberg/respx/pull/278
    """

    def test_fetch_single_entry_returns_one_payload(self):
        """Successful fetch with one entry returns a list of length 1 (B2-R04, R20)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=SINGLE_ENTRY_FEED)
            )
            source = _default_source()
            payloads = source.fetch()

        assert len(payloads) == 1
        assert payloads[0].source_id == "arxiv"
        assert payloads[0].source_type is SourceType.PAPER
        assert payloads[0].external_id == "2401.12345"

    def test_fetch_two_entries_returns_two_payloads(self):
        """Two <entry> elements produce two payloads in order (B2-R04, R20)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=TWO_ENTRY_FEED)
            )
            source = _default_source()
            payloads = source.fetch()

        assert len(payloads) == 2
        assert payloads[0].external_id == "2401.11111"
        assert payloads[1].external_id == "2401.22222"

    def test_fetch_empty_feed_returns_empty_list(self):
        """Empty <feed> returns [] without error (B2-R04)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_FEED)
            )
            result = _default_source().fetch()

        assert result == []

    def test_fetch_uses_configured_max_results(self):
        """max_results from config is sent as a query parameter (B2-R16)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_FEED)
            )
            _source_with_config(max_results=10, categories=["cs.AI"]).fetch()
            # Capture inside the context manager: mock.calls is reset on __exit__.
            call_count = mock.calls.call_count
            query = str(mock.calls.last.request.url.query)

        assert call_count == 1
        assert "max_results=10" in query

    def test_fetch_uses_configured_categories(self):
        """All configured categories appear in the search_query parameter (B2-R16)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_FEED)
            )
            _source_with_config(categories=["cs.AI", "cs.CV"]).fetch()
            query = str(mock.calls.last.request.url.query)

        assert "cs.AI" in query
        assert "cs.CV" in query

    def test_fetch_malformed_xml_returns_empty_list(self):
        """Invalid XML from the API returns [] without raising (B2-R04, R23)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=MALFORMED_XML)
            )
            result = _default_source().fetch()

        assert result == []

    def test_fetch_network_timeout_returns_empty_list(self):
        """TimeoutException after tenacity exhaustion returns [] (B2-R04, R06, R21)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):  # prevent tenacity back-off from slowing the test
                result = _default_source().fetch()

        assert result == []

    def test_fetch_network_timeout_retries_three_times(self):
        """Tenacity makes 3 total attempts on TimeoutException (B2-R05, R06)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_429_rate_limit_returns_empty_list(self):
        """HTTP 429 after tenacity exhaustion returns [] (B2-R04, R08, R22)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                result = _default_source().fetch()

        assert result == []

    def test_fetch_429_retries_three_times(self):
        """Tenacity makes 3 total attempts on HTTP 429 (B2-R05, R08)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_401_returns_empty_list(self):
        """HTTP 401 returns [] immediately (B2-R04, R07)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            result = _default_source().fetch()

        assert result == []

    def test_fetch_401_does_not_retry(self):
        """HTTP 401 makes exactly 1 request — no retry (B2-R07)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(401, text="Unauthorized")
            )
            _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 1

    def test_fetch_500_retries_three_times(self):
        """HTTP 500 is retried by tenacity (transient server error) (B2-R05)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_500_returns_empty_list(self):
        """HTTP 500 after retries returns [] (B2-R04)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                result = _default_source().fetch()

        assert result == []


# ---------------------------------------------------------------------------
# Normalize tests — no HTTP involved
# ---------------------------------------------------------------------------


class TestNormalize:
    """Tests for normalize() (SDS §5.4, B2-R09 through B2-R12, R18)."""

    _VALID_RAW: dict = {
        "external_id": "2401.12345",
        "title": "Attention Is All You Need Again",
        "abstract": "A new transformer architecture that changes everything.",
        "authors": ["Alice Smith", "Bob Jones"],
        "institutions": ["MIT", "Stanford University"],
        "primary_url": "http://arxiv.org/abs/2401.12345v1",
        "pdf_url": "http://arxiv.org/pdf/2401.12345v1",
        "published_date": "2024-01-15",
        "categories": ["cs.LG", "cs.AI"],
        "primary_category": "cs.LG",
    }

    def test_normalize_returns_normalized_item(self):
        """normalize() returns a NormalizedItem for valid raw_data (B2-R09)."""
        from arip.entities import NormalizedItem

        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert isinstance(result, NormalizedItem)

    def test_normalize_source_id(self):
        """source_id is preserved from the payload (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.source_id == "arxiv"

    def test_normalize_source_type(self):
        """source_type value is 'PAPER' (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.source_type == "PAPER"

    def test_normalize_external_id(self):
        """external_id is preserved from the payload (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.external_id == "2401.12345"

    def test_normalize_title(self):
        """Title is extracted correctly (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.title == "Attention Is All You Need Again"

    def test_normalize_abstract(self):
        """Abstract is extracted correctly (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.abstract == "A new transformer architecture that changes everything."

    def test_normalize_authors(self):
        """Authors list is extracted correctly (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.authors == ["Alice Smith", "Bob Jones"]

    def test_normalize_institutions(self):
        """Institutions list is extracted from arxiv:affiliation elements (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.institutions == ["MIT", "Stanford University"]

    def test_normalize_primary_url(self):
        """primary_url is the abs page URL (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.primary_url == "http://arxiv.org/abs/2401.12345v1"

    def test_normalize_additional_urls_contains_pdf(self):
        """PDF URL is placed in additional_urls when present (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.additional_urls == ["http://arxiv.org/pdf/2401.12345v1"]

    def test_normalize_additional_urls_none_when_no_pdf(self):
        """additional_urls is None when no PDF URL in raw_data (B2-R09)."""
        data = {**self._VALID_RAW, "pdf_url": ""}
        payload = _raw_payload_from_dict(data)
        result = ArXivSource(config=None).normalize(payload)
        assert result.additional_urls is None

    def test_normalize_published_date(self):
        """published_date is an ISO date string (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.published_date == "2024-01-15"

    def test_normalize_topics_from_categories(self):
        """topics list is populated from ArXiv categories (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.topics == ["cs.LG", "cs.AI"]

    def test_normalize_language_is_en(self):
        """Language defaults to EN (B2-R09)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.language == "EN"

    def test_normalize_source_signals_is_none(self):
        """source_signals is None — ArXiv provides no engagement metrics (B2-R18)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert result.source_signals is None

    def test_normalize_content_hash_formula(self):
        """content_hash matches SDS §4.2 formula (B2-R10)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)

        expected = hashlib.sha256(
            b"arxiv:2401.12345:Attention Is All You Need Again"
        ).hexdigest()
        assert result.content_hash == expected

    def test_normalize_content_hash_uses_helper(self):
        """content_hash matches compute_content_hash() from _http.py (B2-R10)."""
        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)

        expected = compute_content_hash(
            source_id="arxiv",
            external_id="2401.12345",
            title="Attention Is All You Need Again",
        )
        assert result.content_hash == expected

    def test_normalize_content_hash_truncates_title_at_200(self):
        """content_hash uses only the first 200 characters of the title (SDS §4.2)."""
        long_title = "A" * 300
        # _VALID_RAW contains external_id "2401.12345"; _raw_payload_from_dict
        # reads it via data.get("external_id", ...) so the hash must use that ID.
        data = {**self._VALID_RAW, "title": long_title}
        payload = _raw_payload_from_dict(data)
        result = ArXivSource(config=None).normalize(payload)

        expected = hashlib.sha256(
            f"arxiv:2401.12345:{'A' * 200}".encode()
        ).hexdigest()
        assert result.content_hash == expected

    def test_normalize_raw_payload_is_json_of_raw_data(self):
        """raw_payload is a JSON-serialised copy of raw_data (B2-R09)."""
        import json

        payload = _raw_payload_from_dict(self._VALID_RAW)
        result = ArXivSource(config=None).normalize(payload)
        assert json.loads(result.raw_payload) == self._VALID_RAW

    def test_normalize_missing_title_raises_source_error(self):
        """SourceError raised when title is empty (B2-R11)."""
        data = {**self._VALID_RAW, "title": ""}
        payload = _raw_payload_from_dict(data)
        with pytest.raises(SourceError, match="title"):
            ArXivSource(config=None).normalize(payload)

    def test_normalize_missing_title_key_raises_source_error(self):
        """SourceError raised when title key is absent from raw_data (B2-R11)."""
        data = {k: v for k, v in self._VALID_RAW.items() if k != "title"}
        payload = _raw_payload_from_dict(data)
        with pytest.raises(SourceError, match="title"):
            ArXivSource(config=None).normalize(payload)

    def test_normalize_missing_primary_url_raises_source_error(self):
        """SourceError raised when primary_url is empty (B2-R12)."""
        data = {**self._VALID_RAW, "primary_url": ""}
        payload = _raw_payload_from_dict(data)
        with pytest.raises(SourceError, match="primary_url"):
            ArXivSource(config=None).normalize(payload)

    def test_normalize_missing_primary_url_key_raises_source_error(self):
        """SourceError raised when primary_url key is absent from raw_data (B2-R12)."""
        data = {k: v for k, v in self._VALID_RAW.items() if k != "primary_url"}
        payload = _raw_payload_from_dict(data)
        with pytest.raises(SourceError, match="primary_url"):
            ArXivSource(config=None).normalize(payload)

    def test_normalize_empty_authors_produces_none(self):
        """authors field is None when authors list is empty (B2-R09)."""
        data = {**self._VALID_RAW, "authors": []}
        payload = _raw_payload_from_dict(data)
        result = ArXivSource(config=None).normalize(payload)
        assert result.authors is None

    def test_normalize_empty_institutions_produces_none(self):
        """institutions field is None when institutions list is empty (B2-R09)."""
        data = {**self._VALID_RAW, "institutions": []}
        payload = _raw_payload_from_dict(data)
        result = ArXivSource(config=None).normalize(payload)
        assert result.institutions is None

    def test_normalize_empty_categories_produces_none_topics(self):
        """topics field is None when categories list is empty (B2-R09)."""
        data = {**self._VALID_RAW, "categories": []}
        payload = _raw_payload_from_dict(data)
        result = ArXivSource(config=None).normalize(payload)
        assert result.topics is None

    def test_normalize_none_abstract_stored_as_none(self):
        """abstract field is None when raw_data abstract is empty (B2-R09)."""
        data = {**self._VALID_RAW, "abstract": ""}
        payload = _raw_payload_from_dict(data)
        result = ArXivSource(config=None).normalize(payload)
        assert result.abstract is None

    def test_normalize_none_published_date_stored_as_none(self):
        """published_date field is None when raw_data value is empty (B2-R09)."""
        data = {**self._VALID_RAW, "published_date": ""}
        payload = _raw_payload_from_dict(data)
        result = ArXivSource(config=None).normalize(payload)
        assert result.published_date is None


# ---------------------------------------------------------------------------
# ArXiv ID extraction tests — exercised via fetch round-trip
# ---------------------------------------------------------------------------


class TestArxivIdExtraction:
    """Validate that version suffixes are stripped from ArXiv IDs (B2-R09)."""

    def test_version_suffix_stripped_from_new_style_id(self):
        """'2401.12345v1' external_id becomes '2401.12345' (B2-R09)."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=SINGLE_ENTRY_FEED)
            )
            payloads = _default_source().fetch()
        assert payloads[0].external_id == "2401.12345"

    def test_high_version_number_stripped(self):
        """Version numbers greater than 9 are stripped correctly (B2-R09)."""
        feed = SINGLE_ENTRY_FEED.replace(
            "http://arxiv.org/abs/2401.12345v1",
            "http://arxiv.org/abs/2401.12345v12",
        )
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=feed)
            )
            payloads = _default_source().fetch()
        assert payloads[0].external_id == "2401.12345"

    def test_id_without_version_suffix_preserved(self):
        """An abs URL with no version suffix yields the bare ID (B2-R09)."""
        feed = SINGLE_ENTRY_FEED.replace(
            "http://arxiv.org/abs/2401.12345v1",
            "http://arxiv.org/abs/2401.12345",
        )
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=feed)
            )
            payloads = _default_source().fetch()
        assert payloads[0].external_id == "2401.12345"


# ---------------------------------------------------------------------------
# Payload structure tests — verify raw_data fields set during fetch
# ---------------------------------------------------------------------------


class TestPayloadStructure:
    """Verify that raw_data dict produced by _entry_to_dict contains correct values."""

    def _fetch_single(self) -> RawSourcePayload:
        """Return the first payload from a mocked single-entry fetch."""
        with respx.mock as mock:
            mock.route(url__startswith=ARXIV_BASE_URL).mock(
                return_value=httpx.Response(200, text=SINGLE_ENTRY_FEED)
            )
            return _default_source().fetch()[0]

    def test_raw_data_title(self):
        """raw_data['title'] matches the entry title (B2-R09)."""
        assert self._fetch_single().raw_data["title"] == "Attention Is All You Need Again"

    def test_raw_data_authors(self):
        """raw_data['authors'] contains all author names (B2-R09)."""
        assert self._fetch_single().raw_data["authors"] == ["Alice Smith", "Bob Jones"]

    def test_raw_data_institutions(self):
        """raw_data['institutions'] contains affiliation strings (B2-R09)."""
        assert self._fetch_single().raw_data["institutions"] == [
            "MIT",
            "Stanford University",
        ]

    def test_raw_data_primary_url(self):
        """raw_data['primary_url'] is the alternate-rel link (B2-R09)."""
        assert (
            self._fetch_single().raw_data["primary_url"]
            == "http://arxiv.org/abs/2401.12345v1"
        )

    def test_raw_data_pdf_url(self):
        """raw_data['pdf_url'] is the pdf-titled link (B2-R09)."""
        assert (
            self._fetch_single().raw_data["pdf_url"]
            == "http://arxiv.org/pdf/2401.12345v1"
        )

    def test_raw_data_published_date(self):
        """raw_data['published_date'] is 'YYYY-MM-DD' (B2-R09)."""
        assert self._fetch_single().raw_data["published_date"] == "2024-01-15"

    def test_raw_data_categories(self):
        """raw_data['categories'] contains all category codes (B2-R09)."""
        assert self._fetch_single().raw_data["categories"] == ["cs.LG", "cs.AI"]

    def test_raw_data_primary_category(self):
        """raw_data['primary_category'] is the arxiv:primary_category term (B2-R09)."""
        assert self._fetch_single().raw_data["primary_category"] == "cs.LG"

    def test_raw_data_abstract(self):
        """raw_data['abstract'] is the full summary text (B2-R09)."""
        assert "transformer" in self._fetch_single().raw_data["abstract"]


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """Verify SourceRegistry discovers ArXivSource (SDS §1.2, B2-R14, R15)."""

    def test_registry_includes_arxiv(self, tmp_path: Path):
        """SourceRegistry.get_source('arxiv') returns an ArXivSource instance (B2-R14, R15)."""
        import arip.sources  # noqa: F401 — triggers __init__.py imports

        settings = _minimal_settings(tmp_path)
        registry = SourceRegistry(settings)
        source = registry.get_source("arxiv")

        assert source is not None
        assert isinstance(source, ArXivSource)

    def test_registry_includes_arxiv_in_active_sources(self, tmp_path: Path):
        """ArXivSource appears in get_active_sources() (B2-R15)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        registry = SourceRegistry(settings)
        source_ids = [s.source_id for s in registry.get_active_sources()]

        assert "arxiv" in source_ids

    def test_registry_excludes_arxiv_when_disabled(self, tmp_path: Path):
        """ArXivSource is not in active sources when enabled=False (B2-R17)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        # Disable arxiv directly on the config model
        object.__setattr__(settings.sources, "arxiv", ArxivSourceConfig(enabled=False))
        registry = SourceRegistry(settings)

        assert registry.get_source("arxiv") is None
        source_ids = [s.source_id for s in registry.get_active_sources()]
        assert "arxiv" not in source_ids
