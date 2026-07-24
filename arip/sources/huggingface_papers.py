"""
HuggingFace Papers source plugin for ARIP.

Fetches the daily AI papers featured on HuggingFace Papers
(https://huggingface.co/papers) via the public daily-papers JSON endpoint.

HuggingFace Papers API:
  GET https://huggingface.co/api/daily_papers
  Returns a JSON array of paper objects for the current day's featured papers.
  No authentication required.

Each paper carries an upvote count used as the engagement signal in the
ranking stage (SDS §5.5).

Source authority score: 0.80 (second highest among all sources, per SDS §5.5).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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

HF_DAILY_PAPERS_URL: str = "https://huggingface.co/api/daily_papers"
"""Public endpoint for HuggingFace daily featured papers.

Exposed at module level so tests can reference it without importing the class.
"""

HF_PAPER_BASE_URL: str = "https://huggingface.co/papers"
"""Base URL for individual HuggingFace paper pages.

Primary URL is synthesised as ``f"{HF_PAPER_BASE_URL}/{external_id}"``.
"""

HF_ARXIV_BASE_URL: str = "https://arxiv.org/abs"
"""Base URL for ArXiv abstract pages.

Included in ``additional_urls`` because HuggingFace Papers IDs are ArXiv IDs.
"""


# ---------------------------------------------------------------------------
# Source plugin
# ---------------------------------------------------------------------------


class HuggingFacePapersSource(BaseSource):
    """Source plugin fetching daily featured papers from HuggingFace Papers.

    Each pipeline run retrieves the current day's featured papers from the
    public HuggingFace Papers JSON API.  Each paper entry is converted to a
    :class:`~arip.entities.RawSourcePayload` whose ``raw_data`` dict stores
    all parsed fields so that normalization can be replayed from
    ``raw_source_payloads`` without re-fetching the API.

    Source authority score: 0.80 (SDS §5.5).

    The ``source_signals`` field in the normalised item carries
    ``{"upvotes": <int>}`` for use by the engagement signal in ranking.

    Config:
        Uses the base :class:`~arip.config.SourceConfig` (``enabled``,
        ``fetch_interval_hours``).  No HuggingFace-specific fields are needed.
    """

    source_id: ClassVar[str] = "huggingface_papers"
    """Plugin identifier.  Matches ``sources.huggingface_papers`` in ``settings.yaml``."""

    source_type: ClassVar[SourceType] = SourceType.PAPER
    """All HuggingFace Papers results are academic papers."""

    def __init__(self, config: SourceConfig | None) -> None:
        """Initialise the HuggingFace Papers source.

        Args:
            config: Validated :class:`~arip.config.SourceConfig` from
                :class:`~arip.config.AppSettings`.  May be ``None`` when no
                config block exists in ``settings.yaml`` — built-in defaults
                are used in that case.
        """
        super().__init__(config)
        self._cfg: SourceConfig = config if config is not None else SourceConfig()

    @classmethod
    def get_config_schema(cls) -> type[SourceConfig]:
        """Return the Pydantic config model class for this source.

        HuggingFace Papers requires no source-specific configuration beyond the
        base :class:`~arip.config.SourceConfig` (``enabled``,
        ``fetch_interval_hours``).

        This is a classmethod (not an instance method) so the schema can be
        inspected before the source is instantiated, resolving the
        chicken-and-egg problem described in SDS §1.2.

        Returns:
            :class:`~arip.config.SourceConfig`.
        """
        return SourceConfig

    # ------------------------------------------------------------------
    # Public fetch interface
    # ------------------------------------------------------------------

    def fetch(self) -> list[RawSourcePayload]:
        """Fetch today's featured papers from the HuggingFace Papers API.

        Guarantees a list return — never raises.  Any exception from
        :meth:`_fetch_with_retry` (including after tenacity exhaustion) is
        caught here, logged at ``ERROR``, and replaced by an empty list so the
        pipeline continues with remaining sources.

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`, one per paper.
            Empty if the fetch failed or the API returned no papers.
        """
        try:
            return self._fetch_with_retry()
        except Exception:
            logger.error(
                "huggingface_papers_fetch_failed",
                source_id=self.source_id,
                exc_info=True,
            )
            return []

    @FETCH_RETRY
    def _fetch_with_retry(self) -> list[RawSourcePayload]:
        """Perform the HTTP request with tenacity retry logic.

        Decorated with :data:`~arip.sources._http.FETCH_RETRY`:

        - 3 total attempts (1 original + 2 retries).
        - Exponential back-off: 2 s → 4 s → 8 s (capped at 30 s).
        - Retried on: ``TimeoutException``, ``ConnectError``, HTTP 429 / 5xx.
        - **Not** retried on HTTP 401 — the HuggingFace Papers endpoint is
          public and requires no auth; a 401 is an unexpected server-side
          condition that will not resolve by retrying.

        Returns:
            Parsed list of :class:`~arip.entities.RawSourcePayload`.

        Raises:
            httpx.TimeoutException: After tenacity exhaustion on timeout.
            httpx.HTTPStatusError: After tenacity exhaustion on 429 / 5xx,
                or immediately on other non-retried 4xx responses.
        """
        with build_client() as client:
            response = client.get(HF_DAILY_PAPERS_URL)

            if response.status_code == 401:
                # Unexpected for a public endpoint — return early without retry.
                logger.error(
                    "huggingface_papers_auth_failed",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )
                return []

            if response.status_code == 429:
                logger.warning(
                    "huggingface_papers_rate_limited",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )

            response.raise_for_status()
            return self._parse_response(response.text)

    # ------------------------------------------------------------------
    # JSON parsing
    # ------------------------------------------------------------------

    def _parse_response(self, body: str) -> list[RawSourcePayload]:
        """Parse the HuggingFace Papers JSON response into payload objects.

        Each element of the JSON array becomes one payload.  Elements without
        an extractable ``external_id`` are skipped with a ``WARNING`` log.

        Args:
            body: Raw JSON response body from the HuggingFace Papers API.

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`.  Empty if JSON
            parsing fails, the body is not a list, or no entries have a valid
            ``external_id``.
        """
        try:
            entries = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(
                "huggingface_papers_json_parse_error",
                source_id=self.source_id,
                body_preview=body[:200],
            )
            return []

        if not isinstance(entries, list):
            logger.warning(
                "huggingface_papers_unexpected_response_shape",
                source_id=self.source_id,
                response_type=type(entries).__name__,
            )
            return []

        payloads: list[RawSourcePayload] = []
        fetched_at = datetime.now(tz=timezone.utc)  # noqa: UP017

        for entry in entries:
            raw_data = self._entry_to_dict(entry)
            external_id: str = raw_data.get("external_id", "")
            if not external_id:
                logger.warning(
                    "huggingface_papers_entry_missing_id",
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
            "huggingface_papers_response_parsed",
            source_id=self.source_id,
            entry_count=len(payloads),
        )
        return payloads

    def _entry_to_dict(self, entry: dict) -> dict:
        """Convert a single API response item to a plain dict for storage.

        All fields needed by :meth:`normalize` are extracted here and stored
        verbatim so that normalization can be replayed from
        ``raw_source_payloads`` without re-fetching the API.

        The ``external_id`` is the ArXiv-style paper ID (e.g. ``"2401.12345"``)
        taken from the top-level ``id`` field.  HuggingFace Papers IDs are
        ArXiv IDs, making them stable across revisions.

        Args:
            entry: A single dict from the HuggingFace Papers JSON array.

        Returns:
            Dict with keys:

            - ``external_id`` (str): ArXiv-style paper ID.
            - ``title`` (str): Paper title.
            - ``abstract`` (str): Abstract / summary text.
            - ``authors`` (list[str]): Author display names.
            - ``published_date`` (str): ISO date ``"YYYY-MM-DD"`` (empty if absent).
            - ``upvotes`` (int): Number of upvotes on HuggingFace Papers.
        """
        paper: dict = entry.get("paper", {})

        # External ID: prefer top-level "id"; fall back to nested paper.id.
        external_id: str = (
            str(entry.get("id", "")).strip() or str(paper.get("id", "")).strip()
        )

        # Title: prefer nested paper.title; fall back to top-level title.
        title: str = (
            str(paper.get("title", "")).strip() or str(entry.get("title", "")).strip()
        )

        # Abstract / summary from the nested paper object.
        abstract: str = str(paper.get("summary", "")).strip()

        # Authors: only display names are available; no institution data exposed.
        authors: list[str] = [
            str(a.get("name", "")).strip()
            for a in paper.get("authors", [])
            if a.get("name")
        ]

        # Published date: ISO 8601 datetime → truncate to YYYY-MM-DD.
        raw_date: str = str(
            paper.get("publishedAt", "") or entry.get("publishedAt", "")
        ).strip()
        published_date: str = raw_date[:10] if raw_date else ""

        # Engagement metric for the ranking stage.
        upvotes: int = int(paper.get("upvotes", 0))

        return {
            "external_id": external_id,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "published_date": published_date,
            "upvotes": upvotes,
        }

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:
        """Map a :class:`~arip.entities.RawSourcePayload` to the canonical schema.

        Called by the collection stage for every payload returned by
        :meth:`fetch`.  Raises :class:`~arip.exceptions.SourceError` on
        missing required fields so the caller can mark the item ``FAILED``
        with ``failed_at_stage='NORMALIZATION'``.

        ``primary_url`` is synthesised as
        ``https://huggingface.co/papers/{external_id}``.

        ``additional_urls`` contains the ArXiv abstract page
        (``https://arxiv.org/abs/{external_id}``) because HuggingFace Papers
        IDs are ArXiv IDs, making the ArXiv page a useful canonical reference.

        ``source_signals`` is ``{"upvotes": <int>}`` — the engagement metric
        available from the HuggingFace Papers API, used by the engagement signal
        in the ranking stage (SDS §5.5).

        ``institutions`` is always ``None``: the HuggingFace Papers API does
        not expose author affiliations.

        ``topics`` is always ``None``: the daily papers endpoint does not
        return category tags.

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
                f"HuggingFace Papers entry '{payload.external_id}' is missing "
                "required field: title"
            )

        external_id: str = data.get("external_id", "").strip()
        if not external_id:
            raise SourceError(
                "HuggingFace Papers entry is missing required field: primary_url "
                "(external_id is absent and primary_url cannot be constructed)"
            )

        primary_url: str = f"{HF_PAPER_BASE_URL}/{external_id}"
        arxiv_url: str = f"{HF_ARXIV_BASE_URL}/{external_id}"

        authors: list[str] = data.get("authors", [])
        abstract: str | None = data.get("abstract") or None
        published_date: str | None = data.get("published_date") or None
        upvotes: int = data.get("upvotes", 0)

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
            authors=authors if authors else None,
            institutions=None,  # HuggingFace Papers API does not expose affiliations
            abstract=abstract,
            additional_urls=[arxiv_url],
            published_date=published_date,
            topics=None,  # Daily papers endpoint does not expose category tags
            source_signals={"upvotes": upvotes},
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealth:
        """Perform a lightweight connectivity check against the HF Papers API.

        Verifies that the endpoint is reachable and returns a valid JSON list.
        Best-effort: any exception is caught and surfaced as
        ``is_healthy=False`` so the pipeline can log it and continue.

        Returns:
            :class:`~arip.entities.SourceHealth` with ``is_healthy=True`` on
            success, ``False`` on any network, HTTP, or parsing error.
        """
        try:
            with build_client() as client:
                response = client.get(HF_DAILY_PAPERS_URL)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, list):
                    return SourceHealth(
                        source_id=self.source_id,
                        is_healthy=False,
                        last_error="Unexpected response shape: not a JSON array",
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
            