"""
Unit tests for GitHubTrendingSource.

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

Fixtures are trimmed from a real response captured from the GitHub Search
Repositories API (SDS §5.4 testing strategy: "fixture payloads captured from
real API responses").  The ~80 URL-template fields the real response carries
are omitted; the plugin reads none of them.

Tests assert SDS-required behaviour and the acquisition semantics fixed by
SDS §5.3.1.  Values that §5.3.1 delegates to implementation (window length,
result limit, header composition beyond the token rule) are asserted only as
contracts — e.g. that a bounded page size is sent — not as specific numbers,
so that tuning them does not break the suite.

Test coverage:
  - class attributes: source_id, source_type, get_config_schema.
  - fetch(): single item, two items, empty items, malformed JSON, non-dict
    JSON, missing items key, items not a list, network timeout (retried 3×,
    returns []), HTTP 429 (retried 3×, returns []), HTTP 401 (1 attempt,
    returns []), HTTP 403 (returns []), HTTP 500 (retried 3×, returns []),
    entry without full_name skipped.
  - §5.3.1 acquisition semantics: correct endpoint; recently-created
    selection; ordering by stars descending; bounded single-page request.
  - §5.3.1 auth rule: no Authorization without a token; Bearer with one;
    blank token treated as absent; unauthenticated fetch still works.
  - normalize(): every canonical field, SourceError paths, content_hash.
  - payload structure: extraction from a real-shaped response.
  - registry integration: discovery, active sources, disabled exclusion.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
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
from arip.sources.github_trending import (
    GITHUB_SEARCH_URL,
    GITHUB_TOKEN_ENV_VAR,
    GITHUB_TRENDING_WINDOW_DAYS,
    GitHubTrendingSource,
)
from arip.sources.registry import SourceRegistry

# ---------------------------------------------------------------------------
# JSON response fixtures (trimmed from a real API response)
# ---------------------------------------------------------------------------

_FIRST_ITEM: dict = {
    "id": 1304159264,
    "name": "turbo-fieldfare",
    "full_name": "drumih/turbo-fieldfare",
    "private": False,
    "owner": {"login": "drumih", "id": 8088294, "type": "User"},
    "html_url": "https://github.com/drumih/turbo-fieldfare",
    "description": "Gemma 4 26B-A4B inference in ~2 GB of RAM on any M-series MacBook",
    "fork": False,
    "created_at": "2026-07-17T15:57:54Z",
    "updated_at": "2026-08-12T12:58:56Z",
    "pushed_at": "2026-08-11T17:37:40Z",
    "stargazers_count": 5819,
    "watchers_count": 5819,
    "language": "Swift",
    "forks_count": 341,
    "open_issues_count": 52,
    "topics": ["apple-silicon", "gemma", "llm", "llm-inference", "metal"],
    "visibility": "public",
    "score": 1.0,
}

_SECOND_ITEM: dict = {
    "id": 1319154759,
    "name": "kimi-k3-in-c",
    "full_name": "FareedKhan-dev/kimi-k3-in-c",
    "private": False,
    "owner": {"login": "FareedKhan-dev", "id": 63067900, "type": "User"},
    "html_url": "https://github.com/FareedKhan-dev/kimi-k3-in-c",
    "description": "A 2.78-trillion-parameter Kimi K3 running inference on a single CPU.",
    "fork": False,
    "created_at": "2026-08-01T09:29:38Z",
    "stargazers_count": 5052,
    "language": "C",
    "forks_count": 808,
    "topics": ["c99", "inference-engine", "llm", "quantization"],
    "visibility": "public",
    "score": 1.0,
}


def _envelope(items: list[dict]) -> str:
    """Wrap items in the GitHub search response envelope."""
    return json.dumps(
        {"total_count": len(items), "incomplete_results": False, "items": items}
    )


SINGLE_ITEM_RESPONSE: str = _envelope([_FIRST_ITEM])
TWO_ITEM_RESPONSE: str = _envelope([_FIRST_ITEM, _SECOND_ITEM])
EMPTY_RESPONSE: str = _envelope([])
MALFORMED_JSON: str = "this is not json at all <<<"
NON_DICT_JSON: str = json.dumps(["unexpected", "array"])
MISSING_ITEMS_JSON: str = json.dumps({"total_count": 0})
ITEMS_NOT_LIST_JSON: str = json.dumps({"total_count": 1, "items": {"bad": "shape"}})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_settings(tmp_path: Path):
    """Return an AppSettings with only the required llm.model_name field."""
    p = tmp_path / "settings.yaml"
    p.write_text('llm:\n  model_name: "test-model"\n', encoding="utf-8")
    return load_settings(yaml_path=p)


def _default_source() -> GitHubTrendingSource:
    """GitHubTrendingSource with default SourceConfig (no config block)."""
    return GitHubTrendingSource(config=None)


def _raw_payload_from_dict(data: dict) -> RawSourcePayload:
    """Build a RawSourcePayload for normalize() tests."""
    return RawSourcePayload(
        source_id="github_trending",
        source_type=SourceType.REPO,
        external_id=data.get("external_id", "owner/repo"),
        raw_data=data,
        fetched_at=datetime.now(tz=timezone.utc),  # noqa: UP017
    )


# The raw_data dict produced by _entry_to_dict for the first fixture item.
_VALID_RAW: dict = {
    "external_id": "drumih/turbo-fieldfare",
    "title": "drumih/turbo-fieldfare",
    "description": "Gemma 4 26B-A4B inference in ~2 GB of RAM on any M-series MacBook",
    "html_url": "https://github.com/drumih/turbo-fieldfare",
    "owner": "drumih",
    "stars": 5819,
    "topics": ["apple-silicon", "gemma", "llm", "llm-inference", "metal"],
    "language": "Swift",
    "published_date": "2026-07-17",
}


@pytest.fixture(autouse=True)
def _clear_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no ambient ARIP_GITHUB_TOKEN leaks into tests.

    SDS §5.3.1 makes behaviour depend on whether the environment variable is
    available, so a token present in the developer's or CI shell would
    otherwise make the auth tests non-deterministic.
    """
    monkeypatch.delenv(GITHUB_TOKEN_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# Class attribute tests
# ---------------------------------------------------------------------------


class TestClassAttributes:
    """Verify static class-level declarations required by SDS §5.3."""

    def test_source_id(self):
        """source_id must be 'github_trending' (SDS §5.15, §1.2)."""
        assert GitHubTrendingSource.source_id == "github_trending"

    def test_source_type(self):
        """source_type must be REPO (SDS §4.2 source_type column)."""
        assert GitHubTrendingSource.source_type is SourceType.REPO

    def test_get_config_schema_returns_source_config(self):
        """get_config_schema() must return the plain SourceConfig (SDS §5.15)."""
        assert GitHubTrendingSource.get_config_schema() is SourceConfig

    def test_get_config_schema_callable_without_instance(self):
        """Must be callable on the class without an instance (SDS §1.2)."""
        assert GitHubTrendingSource.get_config_schema() is SourceConfig


# ---------------------------------------------------------------------------
# Fetch tests — all HTTP is mocked with respx
# ---------------------------------------------------------------------------


class TestFetch:
    """Tests for fetch() and _fetch_with_retry() (SDS §5.3).

    Uses url__startswith matching to work around respx issue #277.
    See tests/unit/sources/conftest.py for the full explanation.
    """

    def test_fetch_single_item_returns_one_payload(self):
        """Successful fetch with one item returns a list of length 1."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=SINGLE_ITEM_RESPONSE)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 1
        assert payloads[0].source_id == "github_trending"
        assert payloads[0].source_type is SourceType.REPO
        assert payloads[0].external_id == "drumih/turbo-fieldfare"

    def test_fetch_two_items_returns_two_payloads_in_order(self):
        """Two items produce two payloads in response order."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=TWO_ITEM_RESPONSE)
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 2
        assert payloads[0].external_id == "drumih/turbo-fieldfare"
        assert payloads[1].external_id == "FareedKhan-dev/kimi-k3-in-c"

    def test_fetch_empty_items_returns_empty_list(self):
        """An empty items array returns [] without error."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_RESPONSE)
            )
            assert _default_source().fetch() == []

    def test_fetch_malformed_json_returns_empty_list(self):
        """Invalid JSON from the API returns [] without raising (SDS §5.3)."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=MALFORMED_JSON)
            )
            assert _default_source().fetch() == []

    def test_fetch_non_dict_json_returns_empty_list(self):
        """A bare JSON array (not the search envelope) returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=NON_DICT_JSON)
            )
            assert _default_source().fetch() == []

    def test_fetch_missing_items_key_returns_empty_list(self):
        """An envelope without an 'items' key returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=MISSING_ITEMS_JSON)
            )
            assert _default_source().fetch() == []

    def test_fetch_items_not_a_list_returns_empty_list(self):
        """An 'items' value that is not a list returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=ITEMS_NOT_LIST_JSON)
            )
            assert _default_source().fetch() == []

    def test_fetch_network_timeout_returns_empty_list(self):
        """TimeoutException after tenacity exhaustion returns [] (SDS §5.3)."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):
                assert _default_source().fetch() == []

    def test_fetch_network_timeout_retries_three_times(self):
        """Tenacity makes 3 total attempts on TimeoutException (SDS §5.3)."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                side_effect=httpx.TimeoutException("Connection timed out")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_429_rate_limit_returns_empty_list(self):
        """HTTP 429 after tenacity exhaustion returns [] (SDS §5.3)."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                assert _default_source().fetch() == []

    def test_fetch_429_retries_three_times(self):
        """HTTP 429 is retried, per the SDS §5.3 rate-limit failure mode."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(429, text="Too Many Requests")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_401_returns_empty_list(self):
        """HTTP 401 returns [] (SDS §5.3 auth-failure mode)."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(401, text="Bad credentials")
            )
            assert _default_source().fetch() == []

    def test_fetch_401_does_not_retry(self):
        """HTTP 401 makes exactly 1 request — SDS §5.3: "does not retry"."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(401, text="Bad credentials")
            )
            _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 1

    def test_fetch_403_returns_empty_list(self):
        """GitHub signals exhausted rate limits with 403; fetch still returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(403, text="API rate limit exceeded")
            )
            assert _default_source().fetch() == []

    def test_fetch_403_does_not_retry(self):
        """403 is not in the retryable set — exactly 1 request is made."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(403, text="API rate limit exceeded")
            )
            _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 1

    def test_fetch_500_retries_three_times(self):
        """HTTP 500 is retried by tenacity (transient server error)."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                _default_source().fetch()
            call_count = mock.calls.call_count

        assert call_count == 3

    def test_fetch_500_returns_empty_list(self):
        """HTTP 500 after retries returns []."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            with patch("time.sleep"):
                assert _default_source().fetch() == []

    def test_fetch_item_without_full_name_is_skipped(self):
        """An item with no full_name is skipped; valid items still return."""
        bad_item = {"id": 1, "stargazers_count": 10, "topics": []}
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(
                    200, text=_envelope([bad_item, _FIRST_ITEM])
                )
            )
            payloads = _default_source().fetch()

        assert len(payloads) == 1
        assert payloads[0].external_id == "drumih/turbo-fieldfare"


# ---------------------------------------------------------------------------
# SDS §5.3.1 acquisition semantics
# ---------------------------------------------------------------------------


class TestAcquisitionSemantics:
    """Verify the acquisition semantics mandated by SDS §5.3.1.

    "MUST use the GitHub REST Search Repositories API
     (GET https://api.github.com/search/repositories) to approximate GitHub
     Trending by selecting recently created repositories and ordering the
     results by star count in descending order."
    """

    def _captured_url(self) -> httpx.URL:
        """Return the URL of the single request made by a fetch()."""
        with respx.mock as mock:
            route = mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_RESPONSE)
            )
            _default_source().fetch()
            return route.calls[0].request.url

    def test_uses_search_repositories_endpoint(self):
        """The mandated endpoint is used."""
        url = self._captured_url()
        assert f"{url.scheme}://{url.host}{url.path}" == GITHUB_SEARCH_URL
        assert GITHUB_SEARCH_URL == "https://api.github.com/search/repositories"

    def test_selects_recently_created_repositories(self):
        """Selection is by creation date, not push date or an unfiltered search."""
        assert self._captured_url().params["q"].startswith("created:>")

    def test_creation_cutoff_is_within_the_configured_window(self):
        """The created:> cutoff is the window boundary, computed in UTC."""
        q = self._captured_url().params["q"]
        expected = (
            datetime.now(tz=timezone.utc)  # noqa: UP017
            - timedelta(days=GITHUB_TRENDING_WINDOW_DAYS)
        ).date().isoformat()
        assert q == f"created:>{expected}"

    def test_orders_by_star_count(self):
        """Ordering is by stars, as mandated."""
        assert self._captured_url().params["sort"] == "stars"

    def test_orders_descending(self):
        """Highest star count first, as mandated."""
        assert self._captured_url().params["order"] == "desc"

    def test_requests_a_bounded_page_size(self):
        """A positive page size within the API maximum is requested.

        The exact value is an implementation decision under §5.3.1, so only the
        bound is asserted.
        """
        per_page = int(self._captured_url().params["per_page"])
        assert 0 < per_page <= 100

    def test_single_request_per_fetch(self):
        """No pagination — one request per run (implementation decision, §5.3.1)."""
        with respx.mock as mock:
            route = mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=TWO_ITEM_RESPONSE)
            )
            _default_source().fetch()
            assert route.call_count == 1


# ---------------------------------------------------------------------------
# SDS §5.3.1 authentication rule
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Verify the optional-token rule in SDS §5.3.1.

    "MAY use the optional ARIP_GITHUB_TOKEN for authenticated GitHub API
     requests when the environment variable is available; otherwise it MUST
     operate without authentication."
    """

    def _captured_headers(self) -> httpx.Headers:
        """Return the headers of the single request made by a fetch()."""
        with respx.mock as mock:
            route = mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=EMPTY_RESPONSE)
            )
            _default_source().fetch()
            return route.calls[0].request.headers

    def test_accept_header_always_sent(self):
        """The GitHub media type is always requested."""
        assert self._captured_headers()["accept"] == "application/vnd.github+json"

    def test_no_authorization_header_without_token(self):
        """Without the env var the request is unauthenticated, as mandated."""
        assert "authorization" not in self._captured_headers()

    def test_fetch_succeeds_without_token(self):
        """The source is fully functional unauthenticated."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=SINGLE_ITEM_RESPONSE)
            )
            assert len(_default_source().fetch()) == 1

    def test_bearer_header_sent_when_token_available(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """An available token is used for an authenticated request."""
        monkeypatch.setenv(GITHUB_TOKEN_ENV_VAR, "ghp_exampletoken")
        assert self._captured_headers()["authorization"] == "Bearer ghp_exampletoken"

    def test_blank_token_treated_as_unavailable(self, monkeypatch: pytest.MonkeyPatch):
        """A whitespace-only value is not a usable token."""
        monkeypatch.setenv(GITHUB_TOKEN_ENV_VAR, "   ")
        assert "authorization" not in self._captured_headers()

    def test_token_use_adds_no_configuration_field(self):
        """§5.3.1 forbids new config fields for the token."""
        assert not hasattr(SourceConfig(), "github_token")
        assert GitHubTrendingSource.get_config_schema() is SourceConfig


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
        assert result.source_id == "github_trending"

    def test_normalize_source_type_value(self):
        """source_type value is the string 'REPO' (SDS §4.2)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.source_type == "REPO"

    def test_normalize_external_id_is_github_slug(self):
        """external_id is the GitHub slug (SDS §4.2)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.external_id == "drumih/turbo-fieldfare"

    def test_normalize_title_is_slug(self):
        """Title is the repository slug — GitHub exposes no display name."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.title == "drumih/turbo-fieldfare"

    def test_normalize_primary_url_uses_html_url(self):
        """primary_url is the API-supplied canonical repository URL."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.primary_url == "https://github.com/drumih/turbo-fieldfare"

    def test_normalize_primary_url_falls_back_to_slug(self):
        """primary_url is synthesised from the slug when html_url is absent."""
        data = {**_VALID_RAW, "html_url": ""}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.primary_url == "https://github.com/drumih/turbo-fieldfare"

    def test_normalize_abstract_is_description(self):
        """abstract carries the repository description (SDS §5.4 canonical fields)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.abstract is not None
        assert "Gemma 4" in result.abstract

    def test_normalize_empty_description_produces_none(self):
        """abstract is None when the repository has no description."""
        data = {**_VALID_RAW, "description": ""}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.abstract is None

    def test_normalize_authors_is_owner(self):
        """authors holds the owning user/organisation."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.authors == ["drumih"]

    def test_normalize_empty_owner_produces_none(self):
        """authors is None when no owner could be determined."""
        data = {**_VALID_RAW, "owner": ""}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.authors is None

    def test_normalize_institutions_is_none(self):
        """institutions is always None — GitHub exposes accounts, not affiliations."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.institutions is None

    def test_normalize_additional_urls_is_none(self):
        """additional_urls is always None — no stable secondary URL exists."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.additional_urls is None

    def test_normalize_published_date(self):
        """published_date is an ISO date derived from created_at."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.published_date == "2026-07-17"

    def test_normalize_empty_published_date_produces_none(self):
        """published_date is None when the raw value is empty."""
        data = {**_VALID_RAW, "published_date": ""}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.published_date is None

    def test_normalize_source_signals_has_stars(self):
        """source_signals carries the star count (SDS §4.2 "Stars", §5.5)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.source_signals == {"stars": 5819}

    def test_normalize_source_signals_has_only_stars(self):
        """Only 'stars' is emitted — SDS §4.2 names no other metric here."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert set(result.source_signals) == {"stars"}

    def test_normalize_zero_stars_stored_as_zero(self):
        """Zero stars is stored as 0, not dropped or None."""
        data = {**_VALID_RAW, "stars": 0}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.source_signals == {"stars": 0}

    def test_normalize_language_is_en(self):
        """Language defaults to EN (SDS §5.4)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.language == "EN"

    def test_normalize_topics_from_repository_topics(self):
        """topics are the GitHub repository topics, used as-is."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert result.topics == [
            "apple-silicon",
            "gemma",
            "llm",
            "llm-inference",
            "metal",
        ]

    def test_normalize_empty_topics_produces_none(self):
        """topics is None when the repository has no topics."""
        data = {**_VALID_RAW, "topics": []}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        assert result.topics is None

    def test_normalize_content_hash_formula(self):
        """content_hash matches the SDS §4.2 formula."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        expected = hashlib.sha256(
            b"github_trending:drumih/turbo-fieldfare:drumih/turbo-fieldfare"
        ).hexdigest()
        assert result.content_hash == expected

    def test_normalize_content_hash_uses_shared_helper(self):
        """content_hash matches compute_content_hash() — no duplicated hashing."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        expected = compute_content_hash(
            source_id="github_trending",
            external_id="drumih/turbo-fieldfare",
            title="drumih/turbo-fieldfare",
        )
        assert result.content_hash == expected

    def test_normalize_content_hash_truncates_title_at_200(self):
        """content_hash uses only the first 200 characters of the title (SDS §4.2)."""
        data = {**_VALID_RAW, "title": "X" * 300, "external_id": "o/r"}
        result = _default_source().normalize(_raw_payload_from_dict(data))
        expected = hashlib.sha256(
            f"github_trending:o/r:{'X' * 200}".encode()
        ).hexdigest()
        assert result.content_hash == expected

    def test_normalize_raw_payload_is_json_of_raw_data(self):
        """raw_payload is a JSON-serialised copy of raw_data (SDS §4.2)."""
        result = _default_source().normalize(_raw_payload_from_dict(_VALID_RAW))
        assert json.loads(result.raw_payload) == _VALID_RAW

    def test_normalize_missing_title_raises_source_error(self):
        """SourceError raised when title is empty (SDS §5.4 failure mode)."""
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
# Payload structure tests — verify raw_data produced by _entry_to_dict
# ---------------------------------------------------------------------------


class TestPayloadStructure:
    """Verify the raw_data dict extracted from a real-shaped API response."""

    def _fetch_single(self, body: str = SINGLE_ITEM_RESPONSE) -> RawSourcePayload:
        """Return the first payload from a mocked single-item fetch."""
        with respx.mock as mock:
            mock.route(url__startswith=GITHUB_SEARCH_URL).mock(
                return_value=httpx.Response(200, text=body)
            )
            return _default_source().fetch()[0]

    def test_raw_data_external_id_is_full_name(self):
        """raw_data['external_id'] is the repository slug from full_name."""
        assert self._fetch_single().raw_data["external_id"] == "drumih/turbo-fieldfare"

    def test_raw_data_title_equals_external_id(self):
        """raw_data['title'] mirrors the slug."""
        raw = self._fetch_single().raw_data
        assert raw["title"] == raw["external_id"]

    def test_raw_data_owner_from_nested_login(self):
        """owner is taken from the nested owner.login field."""
        assert self._fetch_single().raw_data["owner"] == "drumih"

    def test_raw_data_owner_falls_back_to_slug_prefix(self):
        """owner falls back to the slug prefix when owner.login is unusable."""
        modified = {**_FIRST_ITEM, "owner": None}
        assert self._fetch_single(_envelope([modified])).raw_data["owner"] == "drumih"

    def test_raw_data_stars(self):
        """raw_data['stars'] is the stargazers_count integer."""
        assert self._fetch_single().raw_data["stars"] == 5819

    def test_raw_data_missing_stars_defaults_to_zero(self):
        """An absent stargazers_count becomes 0 rather than raising."""
        modified = {k: v for k, v in _FIRST_ITEM.items() if k != "stargazers_count"}
        assert self._fetch_single(_envelope([modified])).raw_data["stars"] == 0

    def test_raw_data_null_stars_defaults_to_zero(self):
        """An explicit null stargazers_count becomes 0."""
        modified = {**_FIRST_ITEM, "stargazers_count": None}
        assert self._fetch_single(_envelope([modified])).raw_data["stars"] == 0

    def test_raw_data_description(self):
        """raw_data['description'] carries the repository description."""
        assert "Gemma 4" in self._fetch_single().raw_data["description"]

    def test_raw_data_null_description_becomes_empty_string(self):
        """GitHub returns null for repositories without a description."""
        modified = {**_FIRST_ITEM, "description": None}
        assert self._fetch_single(_envelope([modified])).raw_data["description"] == ""

    def test_raw_data_html_url(self):
        """raw_data['html_url'] is the canonical repository URL."""
        assert (
            self._fetch_single().raw_data["html_url"]
            == "https://github.com/drumih/turbo-fieldfare"
        )

    def test_raw_data_topics(self):
        """raw_data['topics'] is the repository topic list."""
        assert "llm" in self._fetch_single().raw_data["topics"]

    def test_raw_data_language(self):
        """raw_data['language'] is the primary language."""
        assert self._fetch_single().raw_data["language"] == "Swift"

    def test_raw_data_null_language_becomes_empty_string(self):
        """GitHub returns null for repositories with no detected language."""
        modified = {**_FIRST_ITEM, "language": None}
        assert self._fetch_single(_envelope([modified])).raw_data["language"] == ""

    def test_raw_data_published_date_truncated(self):
        """created_at is truncated from ISO 8601 to YYYY-MM-DD."""
        assert self._fetch_single().raw_data["published_date"] == "2026-07-17"

    def test_raw_data_omits_unused_api_fields(self):
        """Only the fields normalize() needs are retained from the response."""
        raw = self._fetch_single().raw_data
        assert "forks_count" not in raw
        assert "score" not in raw
        assert "watchers_count" not in raw

    def test_payload_external_id_matches_raw_data(self):
        """RawSourcePayload.external_id matches raw_data['external_id']."""
        payload = self._fetch_single()
        assert payload.external_id == payload.raw_data["external_id"]

    def test_fetch_then_normalize_round_trip(self):
        """A payload straight from fetch() normalises without error."""
        payload = self._fetch_single()
        result = _default_source().normalize(payload)
        assert result.external_id == "drumih/turbo-fieldfare"
        assert result.source_signals["stars"] == 5819
        assert result.primary_url == "https://github.com/drumih/turbo-fieldfare"


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """Verify SourceRegistry discovers GitHubTrendingSource (SDS §1.2, §5.2)."""

    def test_registry_includes_github_trending(self, tmp_path: Path):
        """get_source('github_trending') returns the correct instance."""
        import arip.sources  # noqa: F401 — triggers __init__.py imports

        registry = SourceRegistry(_minimal_settings(tmp_path))
        source = registry.get_source("github_trending")

        assert source is not None
        assert isinstance(source, GitHubTrendingSource)

    def test_registry_includes_github_trending_in_active_sources(self, tmp_path: Path):
        """GitHubTrendingSource appears in get_active_sources()."""
        import arip.sources  # noqa: F401

        registry = SourceRegistry(_minimal_settings(tmp_path))
        source_ids = [s.source_id for s in registry.get_active_sources()]

        assert "github_trending" in source_ids

    def test_registry_excludes_github_trending_when_disabled(self, tmp_path: Path):
        """Not in active sources when enabled=False (SDS §5.2)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        object.__setattr__(
            settings.sources, "github_trending", SourceConfig(enabled=False)
        )
        registry = SourceRegistry(settings)

        assert registry.get_source("github_trending") is None
        source_ids = [s.source_id for s in registry.get_active_sources()]
        assert "github_trending" not in source_ids

    def test_disabling_github_leaves_other_sources_active(self, tmp_path: Path):
        """Sources are independently toggleable (SDS §5.2)."""
        import arip.sources  # noqa: F401

        settings = _minimal_settings(tmp_path)
        object.__setattr__(
            settings.sources, "github_trending", SourceConfig(enabled=False)
        )
        registry = SourceRegistry(settings)
        source_ids = [s.source_id for s in registry.get_active_sources()]

        assert "github_trending" not in source_ids
        assert "arxiv" in source_ids
        assert "huggingface_models" in source_ids

    def test_all_five_sources_registered(self, tmp_path: Path):
        """Batches 2–5 all present after this batch's registration (SDS §1.2)."""
        import arip.sources  # noqa: F401

        registry = SourceRegistry(_minimal_settings(tmp_path))
        source_ids = [s.source_id for s in registry.get_active_sources()]

        for expected in (
            "arxiv",
            "huggingface_papers",
            "huggingface_models",
            "huggingface_spaces",
            "github_trending",
        ):
            assert expected in source_ids

    def test_source_is_direct_subclass_of_base_source(self):
        """Discovery requires a *direct* BaseSource subclass (SDS §1.2, D-004).

        SourceRegistry scans ``BaseSource.__subclasses__()``, which does not
        recurse, so an intermediate base class would hide this plugin.
        """
        from arip.interfaces import BaseSource

        assert GitHubTrendingSource in BaseSource.__subclasses__()
