"""
GitHub Trending source plugin for ARIP.

GitHub publishes no trending API — https://github.com/trending is an HTML page
with no public JSON endpoint.  SDS §5.3.1 therefore fixes the acquisition
semantics for this source:

    "GitHubTrendingSource MUST use the GitHub REST Search Repositories API
     (GET https://api.github.com/search/repositories) to approximate GitHub
     Trending by selecting recently created repositories and ordering the
     results by star count in descending order."

The response is a JSON *object* — ``{total_count, incomplete_results, items}``
— unlike the HuggingFace endpoints, which return a bare JSON array.

Each repository carries ``stargazers_count``, the metric SDS §4.2 names for
this source ("Stars"), used by the engagement signal in ranking (SDS §5.5).

Source authority score: 0.65 (SDS §5.5).

Implementation decisions delegated by SDS §5.3.1 ("the exact recency window,
result limit, pagination policy, request headers, and other request-level
implementation details are implementation decisions"):

- Recency window: 7 days — see :data:`GITHUB_TRENDING_WINDOW_DAYS`.
- Result limit: 50 — see :data:`GITHUB_TRENDING_LIMIT`.
- Pagination: none; a single request per run, matching every existing source.
- Headers: ``Accept`` always; ``Authorization`` only when a token is present.

Per §5.3.1 none of these introduce configuration fields, and none alter the
``BaseSource``, ``SourceConfig`` or ``SourceRegistry`` contracts.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import structlog

from arip.config import SourceConfig
from arip.entities import NormalizedItem, RawSourcePayload, SourceHealth
from arip.enums import SourceType
from arip.exceptions import SourceError
from arip.interfaces import BaseSource
from arip.sources._http import FETCH_RETRY, build_client, compute_content_hash

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_SEARCH_URL: str = "https://api.github.com/search/repositories"
"""GitHub REST Search Repositories endpoint, mandated by SDS §5.3.1.

Exposed at module level so tests can reference it without importing the class.
"""

GITHUB_REPO_BASE_URL: str = "https://github.com"
"""Base URL for repository pages.

Used only as a fallback when a search result omits ``html_url``; the API
normally supplies the canonical URL directly.
"""

GITHUB_ACCEPT_HEADER: str = "application/vnd.github+json"
"""Media type requested from the GitHub API, per GitHub's documented convention."""

GITHUB_TOKEN_ENV_VAR: str = "ARIP_GITHUB_TOKEN"
"""Environment variable holding the optional GitHub API token.

SDS §5.3.1: the source "MAY use the optional ``ARIP_GITHUB_TOKEN`` for
authenticated GitHub API requests when the environment variable is available;
otherwise it MUST operate without authentication."

Read from the environment rather than from ``AppSettings`` because
``SourceRegistry`` constructs plugins with only their own ``SourceConfig``
block.  §5.3.1 forbids altering that contract to carry the secret.
"""

GITHUB_TRENDING_SORT: str = "stars"
"""Sort key — SDS §5.3.1 mandates ordering by star count."""

GITHUB_TRENDING_ORDER: str = "desc"
"""Sort direction — SDS §5.3.1 mandates descending order."""

GITHUB_TRENDING_WINDOW_DAYS: int = 7
"""Age window defining "recently created" (SDS §5.3.1).

Seven days is chosen because a longer window degrades the approximation: with
a 30-day window the top 50 repositories by star count barely change between
runs, so the source would re-surface the same repositories for weeks.  A
seven-day sliding window rotates meaningfully while still allowing a
repository time to accumulate stars, and the unfiltered search returns far
more than :data:`GITHUB_TRENDING_LIMIT` results at this width.

An implementation decision delegated by §5.3.1, held as a module constant
because §5.15 types ``sources.github_trending`` as a plain ``SourceConfig``
and §5.3.1 forbids introducing new configuration fields.
"""

GITHUB_TRENDING_LIMIT: int = 50
"""Maximum repositories requested per run.

Matches the limit used by the HuggingFace sources and
``pipeline.max_items_per_run``.  The Search API caps ``per_page`` at 100.
Module constant for the same reason as :data:`GITHUB_TRENDING_WINDOW_DAYS`.
"""


# ---------------------------------------------------------------------------
# Source plugin
# ---------------------------------------------------------------------------


class GitHubTrendingSource(BaseSource):
    """Source plugin approximating GitHub trending repositories.

    Each pipeline run queries the GitHub Search Repositories API for
    repositories created within the last :data:`GITHUB_TRENDING_WINDOW_DAYS`
    days, ordered by star count descending (SDS §5.3.1).  Each result becomes a
    :class:`~arip.entities.RawSourcePayload` whose ``raw_data`` dict stores all
    parsed fields so that normalization can be replayed without re-fetching.

    Source authority score: 0.65 (SDS §5.5).

    The ``source_signals`` field in the normalised item carries
    ``{"stars": <int>}`` — the metric SDS §4.2 names for this source — for use
    by the engagement signal in ranking.

    Config:
        Uses the base :class:`~arip.config.SourceConfig` (``enabled``,
        ``fetch_interval_hours``), matching the type declared for
        ``sources.github_trending`` in SDS §5.15.
    """

    source_id: ClassVar[str] = "github_trending"
    """Plugin identifier.  Matches ``sources.github_trending`` in ``settings.yaml``."""

    source_type: ClassVar[SourceType] = SourceType.REPO
    """All GitHub search results are code repositories (SDS §4.2)."""

    def __init__(self, config: SourceConfig | None) -> None:
        """Initialise the GitHub Trending source.

        Args:
            config: Validated :class:`~arip.config.SourceConfig` from
                :class:`~arip.config.AppSettings`.  May be ``None`` when no
                config block exists in ``settings.yaml`` (SDS §5.2) — built-in
                defaults are used in that case.
        """
        super().__init__(config)
        self._cfg: SourceConfig = config if config is not None else SourceConfig()

    @classmethod
    def get_config_schema(cls) -> type[SourceConfig]:
        """Return the Pydantic config model class for this source.

        GitHub Trending requires no source-specific configuration beyond the
        base :class:`~arip.config.SourceConfig`, matching the type declared for
        ``sources.github_trending`` in SDS §5.15.

        This is a classmethod (not an instance method) so the schema can be
        inspected before the source is instantiated, resolving the
        chicken-and-egg problem described in SDS §1.2.

        Returns:
            :class:`~arip.config.SourceConfig`.
        """
        return SourceConfig

    # ------------------------------------------------------------------
    # Request construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_headers() -> dict[str, str]:
        """Build request headers, adding the token only when one is available.

        SDS §5.3.1 permits authenticated requests when ``ARIP_GITHUB_TOKEN`` is
        available and requires unauthenticated operation otherwise.  A blank or
        whitespace-only value is treated as absent, so an empty variable in the
        environment does not produce a malformed ``Authorization`` header.

        Returns:
            Header dict passed to :func:`~arip.sources._http.build_client` as
            ``extra_headers``.  Always contains ``Accept``; contains
            ``Authorization`` only when a non-empty token is present.
        """
        headers: dict[str, str] = {"Accept": GITHUB_ACCEPT_HEADER}
        token = os.environ.get(GITHUB_TOKEN_ENV_VAR, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _build_query() -> str:
        """Build the search query selecting recently created repositories.

        Produces ``created:>YYYY-MM-DD`` where the date is
        :data:`GITHUB_TRENDING_WINDOW_DAYS` days before today (UTC), satisfying
        the "selecting recently created repositories" clause of SDS §5.3.1.
        The window slides with every run, so the result set rotates instead of
        returning the same repositories indefinitely.

        Returns:
            The ``q`` parameter value for the search request.
        """
        cutoff = datetime.now(tz=timezone.utc) - timedelta(  # noqa: UP017
            days=GITHUB_TRENDING_WINDOW_DAYS
        )
        return f"created:>{cutoff.date().isoformat()}"

    # ------------------------------------------------------------------
    # Public fetch interface
    # ------------------------------------------------------------------

    def fetch(self) -> list[RawSourcePayload]:
        """Fetch recently created, highly starred repositories from GitHub.

        Guarantees a list return — never raises.  Any exception from
        :meth:`_fetch_with_retry` (including after tenacity exhaustion) is
        caught here, logged at ``ERROR``, and replaced by an empty list so the
        pipeline continues with remaining sources (SDS §5.3).

        A single request is issued per run; no pagination is performed
        (implementation decision permitted by SDS §5.3.1, matching every
        existing source plugin).

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`, one per
            repository.  Empty if the fetch failed or the search returned no
            results.
        """
        try:
            return self._fetch_with_retry()
        except Exception:
            logger.error(
                "github_trending_fetch_failed",
                source_id=self.source_id,
                exc_info=True,
            )
            return []

    @FETCH_RETRY
    def _fetch_with_retry(self) -> list[RawSourcePayload]:
        """Perform the HTTP request with tenacity retry logic.

        Decorated with :data:`~arip.sources._http.FETCH_RETRY` (SDS §5.3):

        - 3 total attempts (1 original + 2 retries).
        - Exponential back-off: 2 s → 4 s → 8 s (capped at 30 s).
        - Retried on: ``TimeoutException``, ``ConnectError``, HTTP 429 / 5xx.
        - **Not** retried on HTTP 401 — a bad or revoked token will not fix
          itself between attempts (SDS §5.3 auth-failure mode).

        GitHub also returns HTTP 403 when a rate limit is exhausted.  No
        dedicated branch handles it: ``raise_for_status()`` raises
        ``HTTPStatusError``, :func:`~arip.sources._http._is_retryable` returns
        ``False`` for 403, and :meth:`fetch` logs it and returns an empty list
        — already the SDS §5.3 required outcome, so a branch would add a code
        path without changing behaviour.

        Returns:
            Parsed list of :class:`~arip.entities.RawSourcePayload`.

        Raises:
            httpx.TimeoutException: After tenacity exhaustion on timeout.
            httpx.HTTPStatusError: After tenacity exhaustion on 429 / 5xx,
                or immediately on other non-retried 4xx responses.
        """
        params: dict[str, str] = {
            "q": self._build_query(),
            "sort": GITHUB_TRENDING_SORT,
            "order": GITHUB_TRENDING_ORDER,
            "per_page": str(GITHUB_TRENDING_LIMIT),
        }

        with build_client(extra_headers=self._build_headers()) as client:
            response = client.get(GITHUB_SEARCH_URL, params=params)

            if response.status_code == 401:
                # A bad token never recovers between retries — return early.
                logger.error(
                    "github_trending_auth_failed",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )
                return []

            if response.status_code == 429:
                logger.warning(
                    "github_trending_rate_limited",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )

            response.raise_for_status()
            return self._parse_response(response.text)

    # ------------------------------------------------------------------
    # JSON parsing
    # ------------------------------------------------------------------

    def _parse_response(self, body: str) -> list[RawSourcePayload]:
        """Parse the GitHub search JSON response into payload objects.

        The Search API wraps results in an object,
        ``{"total_count": N, "items": [...]}``.  The ``items`` array is
        extracted here; entries without an extractable ``external_id`` are
        skipped with a ``WARNING`` log.

        Args:
            body: Raw JSON response body from the GitHub search API.

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`.  Empty if JSON
            parsing fails, the body is not an object, ``items`` is missing or
            not a list, or no entries have a valid ``external_id``.
        """
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(
                "github_trending_json_parse_error",
                source_id=self.source_id,
                body_preview=body[:200],
            )
            return []

        if not isinstance(document, dict):
            logger.warning(
                "github_trending_unexpected_response_shape",
                source_id=self.source_id,
                response_type=type(document).__name__,
            )
            return []

        entries = document.get("items")
        if not isinstance(entries, list):
            logger.warning(
                "github_trending_missing_items_array",
                source_id=self.source_id,
                items_type=type(entries).__name__,
            )
            return []

        payloads: list[RawSourcePayload] = []
        fetched_at = datetime.now(tz=timezone.utc)  # noqa: UP017

        for entry in entries:
            raw_data = self._entry_to_dict(entry)
            external_id: str = raw_data.get("external_id", "")
            if not external_id:
                logger.warning(
                    "github_trending_entry_missing_id",
                    source_id=self.source_id,
                )
                continue

            payloads.append(
                RawSourcePayload(
                    source_id=self.source_id,
                    source_type=self.source_type,
                    external_id=external_id,
                    raw_data=raw_data,
                    fetched_at=fetched_at,
                )
            )

        logger.debug(
            "github_trending_response_parsed",
            source_id=self.source_id,
            entry_count=len(payloads),
        )
        return payloads

    def _entry_to_dict(self, entry: dict) -> dict:
        """Convert a single search result to a plain dict for storage.

        All fields needed by :meth:`normalize` are extracted here and stored
        verbatim so that normalization can be replayed without re-fetching.
        The full API response carries roughly eighty URL-template fields that
        are never read; only the fields below are retained.

        The ``external_id`` is the repository slug (e.g. ``"owner/repo"``)
        taken from ``full_name`` — SDS §4.2 names "GitHub slug" as the
        source-native ID for this source.

        Args:
            entry: A single dict from the search response's ``items`` array.

        Returns:
            Dict with keys:

            - ``external_id`` (str): Repository slug ``owner/repo``.
            - ``title`` (str): Same as ``external_id``.
            - ``description`` (str): Repository description (empty if absent).
            - ``html_url`` (str): Canonical repository URL (empty if absent).
            - ``owner`` (str): Owning user or organisation login.
            - ``stars`` (int): Stargazer count.
            - ``topics`` (list[str]): Repository topics.
            - ``language`` (str): Primary language (empty if absent).
            - ``published_date`` (str): ISO date ``"YYYY-MM-DD"`` from
              ``created_at`` (empty if absent).
        """
        external_id: str = str(entry.get("full_name", "") or "").strip()

        # Owner: nested login when present, else the slug prefix.
        owner_obj = entry.get("owner")
        owner: str = ""
        if isinstance(owner_obj, dict):
            owner = str(owner_obj.get("login", "") or "").strip()
        if not owner and "/" in external_id:
            owner = external_id.split("/", 1)[0]

        description: str = str(entry.get("description", "") or "").strip()
        html_url: str = str(entry.get("html_url", "") or "").strip()
        language: str = str(entry.get("language", "") or "").strip()

        # Engagement metric for the ranking stage (SDS §4.2 "Stars").
        stars: int = int(entry.get("stargazers_count", 0) or 0)

        topics: list[str] = [str(t) for t in entry.get("topics", []) if t]

        # Creation date: ISO 8601 datetime → truncate to YYYY-MM-DD.
        raw_date: str = str(entry.get("created_at", "") or "").strip()
        published_date: str = raw_date[:10] if raw_date else ""

        return {
            "external_id": external_id,
            "title": external_id,
            "description": description,
            "html_url": html_url,
            "owner": owner,
            "stars": stars,
            "topics": topics,
            "language": language,
            "published_date": published_date,
        }

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:
        """Map a :class:`~arip.entities.RawSourcePayload` to the canonical schema.

        Called by the collection stage for every payload returned by
        :meth:`fetch`.  Raises :class:`~arip.exceptions.SourceError` on missing
        required fields so the caller can mark the item ``FAILED`` with
        ``failed_at_stage='NORMALIZATION'`` (SDS §5.4).

        ``title`` is the repository slug: GitHub exposes no separate display
        name, and the slug is what identifies a repository to a reader.

        ``primary_url`` uses the API-supplied ``html_url``, falling back to
        ``https://github.com/{slug}`` when that field is absent.

        ``abstract`` is the repository description.  Unlike the HuggingFace
        sources, GitHub returns a description on the search endpoint, so no
        follow-up request is needed.

        ``topics`` are GitHub repository topics used as-is: they are already a
        curated, un-namespaced vocabulary, so no filtering step is required.

        ``source_signals`` is ``{"stars": <int>}`` — the metric SDS §4.2 names
        for this source, consumed by the engagement signal in ranking (§5.5).

        ``institutions`` is always ``None``: GitHub exposes an owning account,
        not a verified affiliation.  The owner is recorded in ``authors``.

        Args:
            payload: A :class:`~arip.entities.RawSourcePayload` produced by
                :meth:`fetch`.

        Returns:
            :class:`~arip.entities.NormalizedItem` populated from the API data.

        Raises:
            :class:`~arip.exceptions.SourceError`: If ``title`` is absent, or
                if ``external_id`` is absent (which prevents forming a valid
                ``primary_url``).
        """
        data = payload.raw_data

        title: str = data.get("title", "").strip()
        if not title:
            raise SourceError(
                f"GitHub repository '{payload.external_id}' is missing "
                "required field: title"
            )

        external_id: str = data.get("external_id", "").strip()
        if not external_id:
            raise SourceError(
                "GitHub repository entry is missing required field: primary_url "
                "(external_id is absent and primary_url cannot be constructed)"
            )

        primary_url: str = (
            data.get("html_url") or f"{GITHUB_REPO_BASE_URL}/{external_id}"
        )

        owner: str = data.get("owner", "")
        authors: list[str] | None = [owner] if owner else None

        abstract: str | None = data.get("description") or None
        published_date: str | None = data.get("published_date") or None
        stars: int = data.get("stars", 0)
        topics: list[str] = data.get("topics", [])

        content_hash = compute_content_hash(
            source_id=payload.source_id,
            external_id=external_id,
            title=title,
        )

        return NormalizedItem(
            source_id=payload.source_id,
            source_type=payload.source_type.value,
            external_id=external_id,
            content_hash=content_hash,
            language="EN",
            title=title,
            primary_url=primary_url,
            raw_payload=json.dumps(payload.raw_data),
            authors=authors,
            institutions=None,  # GitHub exposes an account, not an affiliation
            abstract=abstract,
            additional_urls=None,  # No stable secondary URL on the search endpoint
            published_date=published_date,
            topics=topics if topics else None,
            source_signals={"stars": stars},
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealth:
        """Perform a lightweight connectivity check against the search API.

        Requests a single repository (``per_page=1``) and verifies the endpoint
        returns an object containing an ``items`` array.  Best-effort: any
        exception is caught and surfaced as ``is_healthy=False`` so the
        pipeline can log it and continue.

        Returns:
            :class:`~arip.entities.SourceHealth` with ``is_healthy=True`` on
            success, ``False`` on any network, HTTP, or parsing error.
        """
        try:
            with build_client(extra_headers=self._build_headers()) as client:
                response = client.get(
                    GITHUB_SEARCH_URL,
                    params={
                        "q": self._build_query(),
                        "sort": GITHUB_TRENDING_SORT,
                        "order": GITHUB_TRENDING_ORDER,
                        "per_page": "1",
                    },
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(
                    data.get("items"), list
                ):
                    return SourceHealth(
                        source_id=self.source_id,
                        is_healthy=False,
                        last_error=(
                            "Unexpected response shape: no 'items' array in "
                            "search response"
                        ),
                    )
            return SourceHealth(
                source_id=self.source_id,
                is_healthy=True,
            )
        except Exception as exc:
            return SourceHealth(
                source_id=self.source_id,
                is_healthy=False,
                last_error=str(exc),
            )
