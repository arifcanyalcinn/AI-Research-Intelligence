"""
ArXiv source plugin for ARIP.

Fetches recent papers from the ArXiv Atom API (export.arxiv.org).  Categories
and result counts are configurable via :class:`~arip.config.ArxivSourceConfig`.

ArXiv API reference: https://info.arxiv.org/help/api/index.html

Atom namespace used by the feed:
  - Atom standard: ``http://www.w3.org/2005/Atom``
  - ArXiv extension: ``http://arxiv.org/schemas/atom``
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import ClassVar

import structlog

from arip.config import ArxivSourceConfig
from arip.entities import NormalizedItem, RawSourcePayload, SourceHealth
from arip.enums import SourceType
from arip.exceptions import SourceError
from arip.interfaces import BaseSource
from arip.sources._http import FETCH_RETRY, build_client, compute_content_hash

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Namespace constants
# ---------------------------------------------------------------------------

_NS_ATOM: str = "http://www.w3.org/2005/Atom"
_NS_ARXIV: str = "http://arxiv.org/schemas/atom"

_NS: dict[str, str] = {
    "atom": _NS_ATOM,
    "arxiv": _NS_ARXIV,
}
"""Namespace map passed to ElementTree ``find``/``findall`` calls."""

# ---------------------------------------------------------------------------
# ArXiv ID extraction
# ---------------------------------------------------------------------------

_ARXIV_ID_RE: re.Pattern[str] = re.compile(r"/abs/(.+?)(?:v\d+)?$")
"""Extract the version-free ArXiv ID from an abs-page URL.

Examples::

    http://arxiv.org/abs/2401.12345v1  →  "2401.12345"
    http://arxiv.org/abs/math/0601001v2  →  "math/0601001"
    http://arxiv.org/abs/2401.12345   →  "2401.12345"
"""

# ---------------------------------------------------------------------------
# ArXiv source
# ---------------------------------------------------------------------------

ARXIV_BASE_URL: str = "https://export.arxiv.org/api/query"
"""Base URL for the ArXiv Atom API.

Exposed at module level so tests can reference it without importing the class.
"""


class ArXivSource(BaseSource):
    """Source plugin that fetches recent papers from the ArXiv Atom API.

    Each pipeline run queries the configured ArXiv categories, ordered by
    submission date (most recent first).  Each entry is converted to a
    :class:`~arip.entities.RawSourcePayload` whose ``raw_data`` dict contains
    the parsed XML fields for replay without re-fetching.

    Source authority score: 0.85 (highest among all sources, per SDS §5.5).

    Config (via :class:`~arip.config.ArxivSourceConfig`):
        max_results: Maximum papers per run (default 50).
        categories: ArXiv categories to query (default
            ``["cs.AI", "cs.LG", "cs.CL", "cs.CV"]``).
            Combined with ``OR`` in the search query.
    """

    source_id: ClassVar[str] = "arxiv"
    """Plugin identifier.  Matches ``sources.arxiv`` key in ``settings.yaml``."""

    source_type: ClassVar[SourceType] = SourceType.PAPER
    """All ArXiv results are academic papers."""

    def __init__(self, config: ArxivSourceConfig | None) -> None:
        """Initialise the ArXiv source.

        Args:
            config: Validated :class:`~arip.config.ArxivSourceConfig` from
                :class:`~arip.config.AppSettings`.  When ``None`` (no config
                block in ``settings.yaml``), built-in defaults are used.
        """
        super().__init__(config)
        self._cfg: ArxivSourceConfig = (
            config if config is not None else ArxivSourceConfig()
        )

    @classmethod
    def get_config_schema(cls) -> type[ArxivSourceConfig]:
        """Return the Pydantic config model class for this source.

        Called before instantiation so the schema can be inspected without
        creating an instance (resolves the chicken-and-egg problem, SDS §1.2).

        Returns:
            :class:`~arip.config.ArxivSourceConfig`.
        """
        return ArxivSourceConfig

    # ------------------------------------------------------------------
    # Public fetch interface
    # ------------------------------------------------------------------

    def fetch(self) -> list[RawSourcePayload]:
        """Fetch recent papers from the ArXiv Atom API.

        Guarantees a list return — never raises.  Exceptions from
        :meth:`_fetch_with_retry` (after tenacity exhaustion) are caught,
        logged at ``ERROR``, and converted to an empty list so the pipeline
        continues with the remaining sources.

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`, one per paper
            entry.  Empty if the fetch failed or the feed contained no entries.
        """
        try:
            return self._fetch_with_retry()
        except Exception:
            logger.error(
                "arxiv_fetch_failed",
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
        - **Not** retried on HTTP 401 — credentials do not self-heal.
          Returns ``[]`` immediately for 401 without raising.

        HTTP 429 response raises ``httpx.HTTPStatusError`` via
        ``raise_for_status()``.  :func:`~arip.sources._http._is_retryable`
        returns ``True`` for 429 so tenacity retries the request.

        Returns:
            Parsed list of :class:`~arip.entities.RawSourcePayload`.

        Raises:
            httpx.TimeoutException: After tenacity exhaustion on timeout.
            httpx.HTTPStatusError: After tenacity exhaustion on 429 / 5xx,
                or immediately on 4xx that is not retried.
        """
        category_query = " OR ".join(f"cat:{c}" for c in self._cfg.categories)
        params: dict[str, str] = {
            "search_query": category_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": str(self._cfg.max_results),
        }

        with build_client() as client:
            response = client.get(ARXIV_BASE_URL, params=params)

            if response.status_code == 401:
                # Auth failures never recover between retries — return early.
                logger.error(
                    "arxiv_auth_failed",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )
                return []

            if response.status_code == 429:
                logger.warning(
                    "arxiv_rate_limited",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )

            response.raise_for_status()
            return self._parse_atom_feed(response.text)

    # ------------------------------------------------------------------
    # XML parsing
    # ------------------------------------------------------------------

    def _parse_atom_feed(self, xml_text: str) -> list[RawSourcePayload]:
        """Parse the ArXiv Atom XML feed into :class:`~arip.entities.RawSourcePayload` objects.

        Each ``<entry>`` element becomes one payload.  Entries without an
        extractable ``external_id`` are skipped with a ``WARNING`` log.

        Args:
            xml_text: Raw Atom XML response body from the ArXiv API.

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`.  Empty if XML
            parsing fails or the feed contains no entries.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            logger.warning(
                "arxiv_xml_parse_error",
                source_id=self.source_id,
                xml_preview=xml_text[:200],
            )
            return []

        payloads: list[RawSourcePayload] = []
        fetched_at = datetime.now(tz=timezone.utc)  # noqa: UP017

        for entry in root.findall("atom:entry", _NS):
            raw_data = self._entry_to_dict(entry)
            external_id: str = raw_data.get("external_id", "")
            if not external_id:
                logger.warning(
                    "arxiv_entry_missing_id",
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
            "arxiv_feed_parsed",
            source_id=self.source_id,
            entry_count=len(payloads),
        )
        return payloads

    def _entry_to_dict(self, entry: ET.Element) -> dict:
        """Convert a single Atom ``<entry>`` element to a plain dict.

        All fields needed for :meth:`normalize` are extracted here and stored
        verbatim so that normalization can be replayed from
        ``raw_source_payloads`` without re-fetching the API.

        The ``external_id`` is derived from the ``<id>`` URL with the version
        suffix stripped (``2401.12345v1`` → ``"2401.12345"``), producing a
        stable identifier across paper revisions.

        Args:
            entry: A single ``<entry>`` ElementTree element from the Atom feed.

        Returns:
            Dict with keys:

            - ``external_id`` (str): Version-free ArXiv ID.
            - ``title`` (str): Paper title.
            - ``abstract`` (str): Abstract / summary text.
            - ``authors`` (list[str]): Author display names.
            - ``institutions`` (list[str]): Author affiliations (may be empty).
            - ``primary_url`` (str): URL of the abs page.
            - ``pdf_url`` (str): URL of the PDF (empty string if absent).
            - ``published_date`` (str): ISO date ``"YYYY-MM-DD"`` (empty if absent).
            - ``categories`` (list[str]): All ArXiv category codes.
            - ``primary_category`` (str): Primary category code.
        """
        # --- External ID --------------------------------------------------
        id_el = entry.find("atom:id", _NS)
        external_id = ""
        if id_el is not None and id_el.text:
            match = _ARXIV_ID_RE.search(id_el.text)
            if match:
                external_id = match.group(1)

        # --- Title --------------------------------------------------------
        title_el = entry.find("atom:title", _NS)
        title = (title_el.text or "").strip() if title_el is not None else ""

        # --- Abstract -----------------------------------------------------
        summary_el = entry.find("atom:summary", _NS)
        abstract = (summary_el.text or "").strip() if summary_el is not None else ""

        # --- Authors and affiliations -------------------------------------
        authors: list[str] = []
        institutions: list[str] = []
        for author_el in entry.findall("atom:author", _NS):
            name_el = author_el.find("atom:name", _NS)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())
            affil_el = author_el.find("arxiv:affiliation", _NS)
            if affil_el is not None and affil_el.text:
                affil = affil_el.text.strip()
                if affil and affil not in institutions:
                    institutions.append(affil)

        # --- Links --------------------------------------------------------
        primary_url = ""
        pdf_url = ""
        for link_el in entry.findall("atom:link", _NS):
            rel = link_el.get("rel", "")
            href = link_el.get("href", "")
            if rel == "alternate":
                primary_url = href
            elif link_el.get("title") == "pdf":
                pdf_url = href

        # --- Published date -----------------------------------------------
        published_el = entry.find("atom:published", _NS)
        published_date = ""
        if published_el is not None and published_el.text:
            # Value is like "2024-01-15T20:00:00-05:00" — keep only the date.
            published_date = published_el.text.strip()[:10]

        # --- Categories ---------------------------------------------------
        categories: list[str] = [
            cat_el.get("term", "")
            for cat_el in entry.findall("atom:category", _NS)
            if cat_el.get("term")
        ]
        primary_cat_el = entry.find("arxiv:primary_category", _NS)
        primary_category = (
            primary_cat_el.get("term", "") if primary_cat_el is not None else ""
        )

        return {
            "external_id": external_id,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "institutions": institutions,
            "primary_url": primary_url,
            "pdf_url": pdf_url,
            "published_date": published_date,
            "categories": categories,
            "primary_category": primary_category,
        }

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:
        """Map a :class:`~arip.entities.RawSourcePayload` to the canonical schema.

        Called by the collection stage for every payload returned by
        :meth:`fetch`.  Raises :class:`~arip.exceptions.SourceError` on
        missing required fields so the caller can mark the item
        ``FAILED`` with ``failed_at_stage='NORMALIZATION'``.

        The ``content_hash`` is computed with
        :func:`~arip.sources._http.compute_content_hash` using the SDS §4.2
        formula: ``SHA256("{source_id}:{external_id}:{title[:200]}")``.

        ``source_signals`` is ``None`` — the ArXiv public API does not expose
        star counts, download counts, or citation data.  The engagement signal
        will use ``0.0`` for ArXiv items, which is handled gracefully by the
        scorer.

        Args:
            payload: A :class:`~arip.entities.RawSourcePayload` produced by
                :meth:`fetch`.

        Returns:
            :class:`~arip.entities.NormalizedItem` populated from the ArXiv
            entry data.

        Raises:
            :class:`~arip.exceptions.SourceError`: If ``title`` or
                ``primary_url`` are absent from ``payload.raw_data``.
        """
        data = payload.raw_data

        title: str = data.get("title", "").strip()
        if not title:
            raise SourceError(
                f"ArXiv entry '{payload.external_id}' is missing required field: title"
            )

        primary_url: str = data.get("primary_url", "").strip()
        if not primary_url:
            raise SourceError(
                f"ArXiv entry '{payload.external_id}' is missing required field: primary_url"
            )

        authors: list[str] = data.get("authors", [])
        institutions: list[str] = data.get("institutions", [])
        abstract: str | None = data.get("abstract") or None
        published_date: str | None = data.get("published_date") or None
        categories: list[str] = data.get("categories", [])
        pdf_url: str | None = data.get("pdf_url") or None
        additional_urls: list[str] | None = [pdf_url] if pdf_url else None

        content_hash = compute_content_hash(
            source_id=payload.source_id,
            external_id=payload.external_id,
            title=title,
        )

        return NormalizedItem(
            source_id=payload.source_id,
            source_type=payload.source_type.value,
            external_id=payload.external_id,
            content_hash=content_hash,
            language="EN",
            title=title,
            primary_url=primary_url,
            raw_payload=json.dumps(payload.raw_data),
            authors=authors if authors else None,
            institutions=institutions if institutions else None,
            abstract=abstract,
            additional_urls=additional_urls,
            published_date=published_date,
            topics=categories if categories else None,
            source_signals=None,  # ArXiv API exposes no engagement metrics
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealth:
        """Perform a lightweight connectivity check against the ArXiv API.

        Fetches exactly one result from ``cs.AI`` to verify the endpoint is
        reachable.  Best-effort: any exception is caught and surfaced as
        ``is_healthy=False`` so the pipeline can log it and continue.

        Returns:
            :class:`~arip.entities.SourceHealth` with ``is_healthy=True`` on
            success, ``False`` on any network or HTTP error.
        """
        try:
            with build_client() as client:
                response = client.get(
                    ARXIV_BASE_URL,
                    params={"search_query": "cat:cs.AI", "max_results": "1"},
                )
                response.raise_for_status()
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
