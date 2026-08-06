"""
HuggingFace Spaces source plugin for ARIP.

Fetches the most-liked Spaces (hosted demo applications) from the HuggingFace
Hub (https://huggingface.co/spaces) via the public spaces JSON endpoint.

HuggingFace Spaces API:
  GET https://huggingface.co/api/spaces?sort=likes&direction=-1&limit=50
  Returns a JSON array of space objects.  No authentication required.

Each space carries a ``likes`` count used as the engagement signal in the
ranking stage (SDS §5.5).  Unlike models, Spaces have no download metric —
``likes`` is the only engagement figure the Hub exposes for them.

Source authority score: 0.45 (lowest among all sources, per SDS §5.5).

------------------------------------------------------------------------------
Why this module duplicates structure from ``huggingface_models``
------------------------------------------------------------------------------
The Models and Spaces endpoints have a similar response shape, so factoring the
shared fetch/parse scaffolding into a common ``_HuggingFaceListSource`` base
class is an obvious refactor.  It would break plugin discovery.

``SourceRegistry`` discovers plugins via ``BaseSource.__subclasses__()``
(SDS §1.2, Decision D-002).  ``__subclasses__()`` returns **direct** subclasses
only — it does not recurse.  Introducing an intermediate base would mean
``BaseSource.__subclasses__()`` yields that intermediate class (which has no
``source_id``) instead of the two concrete sources, so both would silently
disappear from every pipeline run.

Both source classes therefore inherit from ``BaseSource`` directly, and the
resulting duplication is deliberate.  This matches the existing precedent
between ``arxiv.py`` and ``huggingface_papers.py``, which repeat the same
fetch / 401 / 429 / parse scaffolding for the same reason.
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

HF_SPACES_URL: str = "https://huggingface.co/api/spaces"
"""Public endpoint for the HuggingFace Hub Spaces listing.

Exposed at module level so tests can reference it without importing the class.
"""

HF_SPACE_BASE_URL: str = "https://huggingface.co/spaces"
"""Base URL for individual Space pages.

Primary URL is synthesised as ``f"{HF_SPACE_BASE_URL}/{external_id}"``.  Note
the ``/spaces`` path segment, which models do **not** have.
"""

HF_SPACES_SORT: str = "likes"
"""Sort key requested from the API.

``likes`` is used because the Spaces endpoint exposes no download metric —
it is the only durable popularity figure available for a Space.
"""

HF_SPACES_LIMIT: int = 50
"""Maximum Spaces requested per run.

A module constant rather than a config field: SDS §5.15 types
``sources.huggingface_spaces`` as a plain :class:`~arip.config.SourceConfig`.
Introducing a ``HuggingFaceSpacesSourceConfig`` subclass to make this tunable
would change the frozen config schema, so the value is fixed here.
"""


# ---------------------------------------------------------------------------
# Source plugin
# ---------------------------------------------------------------------------


class HuggingFaceSpacesSource(BaseSource):
    """Source plugin fetching trending Spaces from the HuggingFace Hub.

    Each pipeline run retrieves the most-liked Spaces from the public
    HuggingFace spaces JSON API.  Each entry is converted to a
    :class:`~arip.entities.RawSourcePayload` whose ``raw_data`` dict stores all
    parsed fields so that normalization can be replayed without re-fetching.

    Source authority score: 0.45 (SDS §5.5).

    The ``source_signals`` field in the normalised item carries
    ``{"likes": <int>}`` for use by the engagement signal in ranking.  There is
    deliberately no ``downloads`` key — the Spaces API does not report one, and
    emitting a hardcoded zero would misrepresent a missing metric as a real
    measurement of zero.

    Config:
        Uses the base :class:`~arip.config.SourceConfig` (``enabled``,
        ``fetch_interval_hours``).  No Space-specific fields are needed.
    """

    source_id: ClassVar[str] = "huggingface_spaces"
    """Plugin identifier.  Matches ``sources.huggingface_spaces`` in ``settings.yaml``."""

    source_type: ClassVar[SourceType] = SourceType.SPACE
    """All HuggingFace Spaces results are hosted demo applications."""

    def __init__(self, config: SourceConfig | None) -> None:
        """Initialise the HuggingFace Spaces source.

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

        HuggingFace Spaces requires no source-specific configuration beyond the
        base :class:`~arip.config.SourceConfig` (``enabled``,
        ``fetch_interval_hours``), matching the type declared for
        ``sources.huggingface_spaces`` in SDS §5.15.

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
        """Fetch the most-liked Spaces from the HuggingFace Hub API.

        Guarantees a list return — never raises.  Any exception from
        :meth:`_fetch_with_retry` (including after tenacity exhaustion) is
        caught here, logged at ``ERROR``, and replaced by an empty list so the
        pipeline continues with remaining sources.

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`, one per Space.
            Empty if the fetch failed or the API returned no Spaces.
        """
        try:
            return self._fetch_with_retry()
        except Exception:
            logger.error(
                "huggingface_spaces_fetch_failed",
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
        - **Not** retried on HTTP 401 — the HuggingFace spaces endpoint is
          public and requires no auth; a 401 is an unexpected server-side
          condition that will not resolve by retrying.

        Returns:
            Parsed list of :class:`~arip.entities.RawSourcePayload`.

        Raises:
            httpx.TimeoutException: After tenacity exhaustion on timeout.
            httpx.HTTPStatusError: After tenacity exhaustion on 429 / 5xx,
                or immediately on other non-retried 4xx responses.
        """
        params: dict[str, str] = {
            "sort": HF_SPACES_SORT,
            "direction": "-1",
            "limit": str(HF_SPACES_LIMIT),
        }

        with build_client() as client:
            response = client.get(HF_SPACES_URL, params=params)

            if response.status_code == 401:
                # Unexpected for a public endpoint — return early without retry.
                logger.error(
                    "huggingface_spaces_auth_failed",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )
                return []

            if response.status_code == 429:
                logger.warning(
                    "huggingface_spaces_rate_limited",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )

            response.raise_for_status()
            return self._parse_response(response.text)

    # ------------------------------------------------------------------
    # JSON parsing
    # ------------------------------------------------------------------

    def _parse_response(self, body: str) -> list[RawSourcePayload]:
        """Parse the HuggingFace spaces JSON response into payload objects.

        Each element of the JSON array becomes one payload.  Elements without
        an extractable ``external_id`` are skipped with a ``WARNING`` log.

        Args:
            body: Raw JSON response body from the HuggingFace spaces API.

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`.  Empty if JSON
            parsing fails, the body is not a list, or no entries have a valid
            ``external_id``.
        """
        try:
            entries = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(
                "huggingface_spaces_json_parse_error",
                source_id=self.source_id,
                body_preview=body[:200],
            )
            return []

        if not isinstance(entries, list):
            logger.warning(
                "huggingface_spaces_unexpected_response_shape",
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
                    "huggingface_spaces_entry_missing_id",
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
            "huggingface_spaces_response_parsed",
            source_id=self.source_id,
            entry_count=len(payloads),
        )
        return payloads

    def _entry_to_dict(self, entry: dict) -> dict:
        """Convert a single API response item to a plain dict for storage.

        All fields needed by :meth:`normalize` are extracted here and stored
        verbatim so that normalization can be replayed without re-fetching.

        The ``external_id`` is the Space repo ID (e.g. ``"enzostvs/deepsite"``)
        taken from the top-level ``id`` field.

        Title resolution prefers ``cardData.title`` — the human-readable name
        the Space author sets in the Space's README front-matter (e.g.
        ``"Wan2.2 14B Fast Preview"``) — and falls back to the repo ID.  The
        compact list endpoint usually omits ``cardData``, so the fallback is
        the common path; the preference exists so richer responses are used
        when available.

        Args:
            entry: A single dict from the HuggingFace spaces JSON array.

        Returns:
            Dict with keys:

            - ``external_id`` (str): Space repo ID, e.g. ``"org/name"``.
            - ``title`` (str): Card title when present, else the repo ID.
            - ``author`` (str): Owning user or organisation (empty if absent).
            - ``likes`` (int): Number of likes on the Hub.
            - ``sdk`` (str): Runtime SDK, e.g. ``"gradio"``, ``"docker"``.
            - ``tags`` (list[str]): Raw tag strings, unfiltered.
            - ``published_date`` (str): ISO date ``"YYYY-MM-DD"`` (empty if absent).
        """
        external_id: str = str(entry.get("id", "")).strip()

        # Author: explicit field when present, else the org prefix of the repo ID.
        author: str = str(entry.get("author", "")).strip()
        if not author and "/" in external_id:
            author = external_id.split("/", 1)[0]

        # Title: prefer the human-readable card title; fall back to the repo ID.
        card_data = entry.get("cardData")
        card_title: str = ""
        if isinstance(card_data, dict):
            card_title = str(card_data.get("title", "") or "").strip()
        title: str = card_title or external_id

        # Engagement metric for the ranking stage.  Spaces report no downloads.
        likes: int = int(entry.get("likes", 0) or 0)

        sdk: str = str(entry.get("sdk", "") or "").strip()
        tags: list[str] = [str(t) for t in entry.get("tags", []) if t]

        # Creation date: ISO 8601 datetime → truncate to YYYY-MM-DD.
        raw_date: str = str(entry.get("createdAt", "") or "").strip()
        published_date: str = raw_date[:10] if raw_date else ""

        return {
            "external_id": external_id,
            "title": title,
            "author": author,
            "likes": likes,
            "sdk": sdk,
            "tags": tags,
            "published_date": published_date,
        }

    @staticmethod
    def _extract_topics(tags: list[str], sdk: str) -> list[str]:
        """Select the topical subset of a Space's tags.

        HuggingFace mixes two kinds of tag in one list:

        - **Namespaced** (``key:value``) — infrastructure and provenance
          metadata such as ``region:us``, ``license:mit``, ``modality:text``.
        - **Bare** — genuinely topical labels such as ``gradio``,
          ``leaderboard``, ``mcp-server``.

        Only bare tags are kept, because the ranking stage's topic signal
        (SDS §5.5) matches configured keywords against this list and the
        namespaced values would contribute noise rather than signal.

        ``sdk`` is prepended when it is not already present, since it is the
        primary technical descriptor the Hub assigns to a Space.

        Args:
            tags: Raw tag strings from the API.
            sdk: The Space's runtime SDK (may be empty).

        Returns:
            Ordered, de-duplicated list of topical tags.  Empty if none remain.
        """
        topics: list[str] = []
        if sdk:
            topics.append(sdk)
        for tag in tags:
            if ":" in tag:
                continue  # namespaced metadata, not a topic
            if tag not in topics:
                topics.append(tag)
        return topics

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def normalize(self, payload: RawSourcePayload) -> NormalizedItem:
        """Map a :class:`~arip.entities.RawSourcePayload` to the canonical schema.

        Called by the collection stage for every payload returned by
        :meth:`fetch`.  Raises :class:`~arip.exceptions.SourceError` on missing
        required fields so the caller can mark the item ``FAILED`` with
        ``failed_at_stage='NORMALIZATION'``.

        ``primary_url`` is synthesised as
        ``https://huggingface.co/spaces/{repo_id}`` — note the ``/spaces``
        segment, which distinguishes a Space URL from a model URL.

        ``source_signals`` is ``{"likes": <int>}``.  The Spaces API reports no
        download count, so no ``downloads`` key is emitted; the engagement
        signal in ranking (SDS §5.5) normalises within a source's own batch and
        does not require every source to expose the same metric names.

        ``abstract`` is always ``None``: the Spaces list endpoint returns no
        description.

        ``institutions`` is always ``None``: the Hub exposes an owning account,
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
                f"HuggingFace Space '{payload.external_id}' is missing "
                "required field: title"
            )

        external_id: str = data.get("external_id", "").strip()
        if not external_id:
            raise SourceError(
                "HuggingFace Space entry is missing required field: primary_url "
                "(external_id is absent and primary_url cannot be constructed)"
            )

        primary_url: str = f"{HF_SPACE_BASE_URL}/{external_id}"

        author: str = data.get("author", "")
        authors: list[str] | None = [author] if author else None

        published_date: str | None = data.get("published_date") or None
        likes: int = data.get("likes", 0)

        topics = self._extract_topics(
            tags=data.get("tags", []),
            sdk=data.get("sdk", ""),
        )

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
            institutions=None,  # Hub exposes an owning account, not an affiliation
            abstract=None,  # Spaces list endpoint returns no description
            additional_urls=None,  # No stable secondary URL on the list endpoint
            published_date=published_date,
            topics=topics if topics else None,
            source_signals={"likes": likes},
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealth:
        """Perform a lightweight connectivity check against the HF spaces API.

        Requests a single Space (``limit=1``) and verifies the endpoint returns
        a valid JSON list.  Best-effort: any exception is caught and surfaced
        as ``is_healthy=False`` so the pipeline can log it and continue.

        Returns:
            :class:`~arip.entities.SourceHealth` with ``is_healthy=True`` on
            success, ``False`` on any network, HTTP, or parsing error.
        """
        try:
            with build_client() as client:
                response = client.get(HF_SPACES_URL, params={"limit": "1"})
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
            