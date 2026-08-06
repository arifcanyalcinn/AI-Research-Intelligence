"""
HuggingFace Models source plugin for ARIP.

Fetches the most-downloaded models from the HuggingFace Hub
(https://huggingface.co/models) via the public models JSON endpoint.

HuggingFace Models API:
  GET https://huggingface.co/api/models?sort=downloads&direction=-1&limit=50
  Returns a JSON array of model objects.  No authentication required.

Each model carries ``downloads`` and ``likes`` counts used as the engagement
signal in the ranking stage (SDS §5.5).

Source authority score: 0.55 (SDS §5.5).

Note on ``full=true``: the API supports a ``full=true`` parameter that adds a
``siblings`` array listing every file in the repository.  For popular models
this array contains hundreds of entries and inflates the response by orders of
magnitude while contributing nothing to normalization.  The compact (default)
listing is used deliberately.
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

HF_MODELS_URL: str = "https://huggingface.co/api/models"
"""Public endpoint for the HuggingFace Hub model listing.

Exposed at module level so tests can reference it without importing the class.
"""

HF_MODEL_BASE_URL: str = "https://huggingface.co"
"""Base URL for individual model pages.

Primary URL is synthesised as ``f"{HF_MODEL_BASE_URL}/{external_id}"`` because
a model's repo ID (``org/name``) is also its URL path.
"""

HF_MODELS_SORT: str = "downloads"
"""Sort key requested from the API.

``downloads`` surfaces the models with the widest real-world adoption, which is
the signal this source exists to capture.  Ranking re-scores everything
afterwards (SDS §5.5); this only decides which slice of the Hub is retrieved.
"""

HF_MODELS_LIMIT: int = 50
"""Maximum models requested per run.

A module constant rather than a config field: SDS §5.15 types
``sources.huggingface_models`` as a plain :class:`~arip.config.SourceConfig`.
Introducing a ``HuggingFaceModelsSourceConfig`` subclass to make this tunable
would change the frozen config schema, so the value is fixed here.
"""


# ---------------------------------------------------------------------------
# Source plugin
# ---------------------------------------------------------------------------


class HuggingFaceModelsSource(BaseSource):
    """Source plugin fetching trending models from the HuggingFace Hub.

    Each pipeline run retrieves the most-downloaded models from the public
    HuggingFace models JSON API.  Each model entry is converted to a
    :class:`~arip.entities.RawSourcePayload` whose ``raw_data`` dict stores all
    parsed fields so that normalization can be replayed without re-fetching.

    Source authority score: 0.55 (SDS §5.5).

    The ``source_signals`` field in the normalised item carries
    ``{"downloads": <int>, "likes": <int>}`` for use by the engagement signal in
    ranking.

    This class inherits from :class:`~arip.interfaces.BaseSource` *directly*.
    It deliberately shares no intermediate base class with
    :class:`~arip.sources.huggingface_spaces.HuggingFaceSpacesSource` despite
    the similar API shape — see the module docstring of ``huggingface_spaces``
    for the discovery constraint that forbids it.

    Config:
        Uses the base :class:`~arip.config.SourceConfig` (``enabled``,
        ``fetch_interval_hours``).  No model-specific fields are needed.
    """

    source_id: ClassVar[str] = "huggingface_models"
    """Plugin identifier.  Matches ``sources.huggingface_models`` in ``settings.yaml``."""

    source_type: ClassVar[SourceType] = SourceType.MODEL
    """All HuggingFace Hub model results are models."""

    def __init__(self, config: SourceConfig | None) -> None:
        """Initialise the HuggingFace Models source.

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

        HuggingFace Models requires no source-specific configuration beyond the
        base :class:`~arip.config.SourceConfig` (``enabled``,
        ``fetch_interval_hours``), matching the type declared for
        ``sources.huggingface_models`` in SDS §5.15.

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
        """Fetch the most-downloaded models from the HuggingFace Hub API.

        Guarantees a list return — never raises.  Any exception from
        :meth:`_fetch_with_retry` (including after tenacity exhaustion) is
        caught here, logged at ``ERROR``, and replaced by an empty list so the
        pipeline continues with remaining sources.

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`, one per model.
            Empty if the fetch failed or the API returned no models.
        """
        try:
            return self._fetch_with_retry()
        except Exception:
            logger.error(
                "huggingface_models_fetch_failed",
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
        - **Not** retried on HTTP 401 — the HuggingFace models endpoint is
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
            "sort": HF_MODELS_SORT,
            "direction": "-1",
            "limit": str(HF_MODELS_LIMIT),
        }

        with build_client() as client:
            response = client.get(HF_MODELS_URL, params=params)

            if response.status_code == 401:
                # Unexpected for a public endpoint — return early without retry.
                logger.error(
                    "huggingface_models_auth_failed",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )
                return []

            if response.status_code == 429:
                logger.warning(
                    "huggingface_models_rate_limited",
                    source_id=self.source_id,
                    status_code=response.status_code,
                )

            response.raise_for_status()
            return self._parse_response(response.text)

    # ------------------------------------------------------------------
    # JSON parsing
    # ------------------------------------------------------------------

    def _parse_response(self, body: str) -> list[RawSourcePayload]:
        """Parse the HuggingFace models JSON response into payload objects.

        Each element of the JSON array becomes one payload.  Elements without
        an extractable ``external_id`` are skipped with a ``WARNING`` log.

        Args:
            body: Raw JSON response body from the HuggingFace models API.

        Returns:
            List of :class:`~arip.entities.RawSourcePayload`.  Empty if JSON
            parsing fails, the body is not a list, or no entries have a valid
            ``external_id``.
        """
        try:
            entries = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(
                "huggingface_models_json_parse_error",
                source_id=self.source_id,
                body_preview=body[:200],
            )
            return []

        if not isinstance(entries, list):
            logger.warning(
                "huggingface_models_unexpected_response_shape",
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
                    "huggingface_models_entry_missing_id",
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
            "huggingface_models_response_parsed",
            source_id=self.source_id,
            entry_count=len(payloads),
        )
        return payloads

    def _entry_to_dict(self, entry: dict) -> dict:
        """Convert a single API response item to a plain dict for storage.

        All fields needed by :meth:`normalize` are extracted here and stored
        verbatim so that normalization can be replayed without re-fetching.

        The ``external_id`` is the model repo ID (e.g.
        ``"sentence-transformers/all-MiniLM-L6-v2"``) taken from the top-level
        ``id`` field, falling back to ``modelId``.  Repo IDs are stable and are
        also the URL path, so they double as the primary-URL suffix.

        Args:
            entry: A single dict from the HuggingFace models JSON array.

        Returns:
            Dict with keys:

            - ``external_id`` (str): Model repo ID, e.g. ``"org/name"``.
            - ``title`` (str): Same as ``external_id`` — the Hub exposes no
              separate display name on the list endpoint.
            - ``author`` (str): Owning user or organisation (empty if absent).
            - ``downloads`` (int): Download count reported by the Hub.
            - ``likes`` (int): Number of likes on the Hub.
            - ``tags`` (list[str]): Raw tag strings, unfiltered.
            - ``pipeline_tag`` (str): Task category, e.g. ``"text-generation"``.
            - ``library_name`` (str): Framework, e.g. ``"transformers"``.
            - ``published_date`` (str): ISO date ``"YYYY-MM-DD"`` (empty if absent).
        """
        # External ID: prefer top-level "id"; fall back to "modelId".
        external_id: str = (
            str(entry.get("id", "")).strip() or str(entry.get("modelId", "")).strip()
        )

        # Author: explicit field when present, else the org prefix of the repo ID.
        author: str = str(entry.get("author", "")).strip()
        if not author and "/" in external_id:
            author = external_id.split("/", 1)[0]

        # Engagement metrics for the ranking stage.
        downloads: int = int(entry.get("downloads", 0) or 0)
        likes: int = int(entry.get("likes", 0) or 0)

        tags: list[str] = [str(t) for t in entry.get("tags", []) if t]
        pipeline_tag: str = str(entry.get("pipeline_tag", "") or "").strip()
        library_name: str = str(entry.get("library_name", "") or "").strip()

        # Creation date: ISO 8601 datetime → truncate to YYYY-MM-DD.
        raw_date: str = str(entry.get("createdAt", "") or "").strip()
        published_date: str = raw_date[:10] if raw_date else ""

        return {
            "external_id": external_id,
            "title": external_id,
            "author": author,
            "downloads": downloads,
            "likes": likes,
            "tags": tags,
            "pipeline_tag": pipeline_tag,
            "library_name": library_name,
            "published_date": published_date,
        }

    @staticmethod
    def _extract_topics(tags: list[str], pipeline_tag: str) -> list[str]:
        """Select the topical subset of a model's tags.

        HuggingFace mixes two kinds of tag in one list:

        - **Namespaced** (``key:value``) — infrastructure and provenance
          metadata such as ``license:apache-2.0``, ``dataset:squad``,
          ``arxiv:1904.06472``, ``base_model:bert-base``, ``region:us``.
        - **Bare** — genuinely topical labels such as ``text-generation``,
          ``transformers``, ``bert``, ``pytorch``.

        Only bare tags are kept, because the ranking stage's topic signal
        (SDS §5.5) matches configured keywords against this list and the
        namespaced values would contribute noise rather than signal.

        ``pipeline_tag`` is prepended when it is not already present, since it
        is the single most descriptive label the Hub assigns.

        Args:
            tags: Raw tag strings from the API.
            pipeline_tag: The model's task category (may be empty).

        Returns:
            Ordered, de-duplicated list of topical tags.  Empty if none remain.
        """
        topics: list[str] = []
        if pipeline_tag:
            topics.append(pipeline_tag)
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

        ``title`` is the model repo ID: the list endpoint exposes no separate
        display name, and the repo ID is what the Hub itself shows as the
        model's name.

        ``primary_url`` is synthesised as ``https://huggingface.co/{repo_id}``.

        ``source_signals`` is ``{"downloads": <int>, "likes": <int>}`` — both
        engagement metrics the Hub exposes, used by the engagement signal in
        the ranking stage (SDS §5.5).

        ``abstract`` is always ``None``: the model list endpoint returns no
        description.  The full model card would require one extra HTTP request
        per model, which is out of scope for this batch.

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
                f"HuggingFace model '{payload.external_id}' is missing "
                "required field: title"
            )

        external_id: str = data.get("external_id", "").strip()
        if not external_id:
            raise SourceError(
                "HuggingFace model entry is missing required field: primary_url "
                "(external_id is absent and primary_url cannot be constructed)"
            )

        primary_url: str = f"{HF_MODEL_BASE_URL}/{external_id}"

        author: str = data.get("author", "")
        authors: list[str] | None = [author] if author else None

        published_date: str | None = data.get("published_date") or None
        downloads: int = data.get("downloads", 0)
        likes: int = data.get("likes", 0)

        topics = self._extract_topics(
            tags=data.get("tags", []),
            pipeline_tag=data.get("pipeline_tag", ""),
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
            abstract=None,  # Model list endpoint returns no description
            additional_urls=None,  # No stable secondary URL on the list endpoint
            published_date=published_date,
            topics=topics if topics else None,
            source_signals={"downloads": downloads, "likes": likes},
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealth:
        """Perform a lightweight connectivity check against the HF models API.

        Requests a single model (``limit=1``) and verifies the endpoint returns
        a valid JSON list.  Best-effort: any exception is caught and surfaced
        as ``is_healthy=False`` so the pipeline can log it and continue.

        Returns:
            :class:`~arip.entities.SourceHealth` with ``is_healthy=True`` on
            success, ``False`` on any network, HTTP, or parsing error.
        """
        try:
            with build_client() as client:
                response = client.get(HF_MODELS_URL, params={"limit": "1"})
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
            