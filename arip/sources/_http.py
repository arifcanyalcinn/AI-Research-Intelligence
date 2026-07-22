"""
Shared HTTP utilities for ARIP source plugins.

Every source plugin uses these three exports:

  build_client()          — pre-configured httpx.Client (timeout, user-agent,
                            redirect-following)
  compute_content_hash()  — deterministic SHA-256 deduplication key per SDS §4.2
  FETCH_RETRY             — tenacity retry decorator (3 attempts, exponential
                            back-off, retries on transient network / 5xx / 429)

Usage in a source plugin::

    from arip.sources._http import build_client, compute_content_hash, FETCH_RETRY

    class MySource(BaseSource):
        def fetch(self) -> list[RawSourcePayload]:
            try:
                return self._fetch_with_retry()
            except Exception as exc:
                logger.error("fetch_failed", source_id=self.source_id, error=str(exc))
                return []

        @FETCH_RETRY
        def _fetch_with_retry(self) -> list[RawSourcePayload]:
            with build_client() as client:
                resp = client.get("https://api.example.com/items")
                resp.raise_for_status()
                ...
"""

from __future__ import annotations

import hashlib

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = "ARIP/0.1.0 (AI Research Intelligence Platform)"

# SDS §7.2: 10 s connect, 30 s read
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# ---------------------------------------------------------------------------
# HTTP client factory
# ---------------------------------------------------------------------------


def build_client(
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Client:
    """Return a pre-configured httpx.Client for source HTTP requests.

    The caller is responsible for closing the client.  Use it as a context
    manager to guarantee cleanup::

        with build_client() as client:
            resp = client.get(url)

    Args:
        timeout: Request timeout.  Defaults to 10 s connect / 30 s read.
        extra_headers: Additional headers merged on top of the default
                       User-Agent header.

    Returns:
        A ready-to-use ``httpx.Client``.
    """
    headers: dict[str, str] = {"User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    return httpx.Client(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# Deduplication hash
# ---------------------------------------------------------------------------


def compute_content_hash(source_id: str, external_id: str, title: str) -> str:
    """Compute the deterministic SHA-256 deduplication hash for an item.

    Formula from SDS §4.2::

        SHA256(f"{source_id}:{external_id}:{title[:200]}")

    The result is stored in ``items.content_hash`` and must be identical
    for two payloads describing the same item regardless of fetch time or
    minor metadata differences.

    Args:
        source_id: Source plugin identifier, e.g. ``"arxiv"``.
        external_id: Source-native item ID, e.g. ``"2401.12345"``.
        title: Item title; only the first 200 characters are used.

    Returns:
        Lowercase hex-encoded SHA-256 digest (64 hex characters).
    """
    raw = f"{source_id}:{external_id}:{title[:200]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------


def _is_retryable(exc: BaseException) -> bool:
    """Return True for transient errors worth retrying.

    Retried:
    - Network timeouts (``httpx.TimeoutException``)
    - Connection errors (``httpx.ConnectError``)
    - HTTP 429, 500, 502, 503, 504

    Not retried:
    - HTTP 401, 403 — credentials won't fix themselves between attempts.
    - HTTP 404      — the resource doesn't exist.
    - Any non-HTTP exception — callers handle those separately.
    """
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return False


FETCH_RETRY = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
"""Tenacity retry decorator for source fetch methods.

3 attempts total (1 original + 2 retries).  Exponential back-off starting at
2 s, capped at 30 s.  Re-raises the final exception after exhaustion so that
``fetch()`` can catch it, log it, and return an empty list — ensuring one
source failure never stops the pipeline.

Apply to the inner ``_fetch_with_retry`` helper, *not* to ``fetch()`` itself,
so that ``fetch()`` can always return ``[]`` on failure::

    @FETCH_RETRY
    def _fetch_with_retry(self) -> list[RawSourcePayload]:
        ...

    def fetch(self) -> list[RawSourcePayload]:
        try:
            return self._fetch_with_retry()
        except Exception as exc:
            logger.error(...)
            return []
"""
