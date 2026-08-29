"""
Scorer — composite importance scoring for collected items.

Implements the four CPU-only signals of SDS §5.5, with the operational values
fixed by §5.5.1 and the decay constant sourced from `ranking.recency_k`
(§5.15.1).

    score = w_recency·recency + w_authority·authority
          + w_engagement·engagement + w_topic·topic

Weights come from `config.ranking.weights` and are validated at startup to sum
to 1.0 ± 0.001 (`config.py::SignalWeights`), so the composite is bounded [0, 1].

Signals
-------

* **Recency** — `exp(-k × days_elapsed)`, `k = config.ranking.recency_k`.
  A missing `published_date` scores 0.0 (§5.5.1: missing data earns no credit).
* **Source authority** — static per-source weight from
  `config.ranking.source_authority`.
* **Engagement** — percentile of the source's engagement metric within its
  cohort for this run (§5.5.1). A source with no engagement metric, or an item
  whose `source_signals` is absent or unparseable, scores 0.0 (§5.5).
* **Topic relevance** — `min(matches / 3, 1.0)` over lowercase substring
  matches of `config.ranking.topic_keywords` (§5.5.1).

Novelty is deliberately absent: AD-19 removes it from the ranking score. It is
a byproduct of semantic deduplication in a later phase.

This module performs no I/O and holds no state between calls. It reads
attributes off `Item` ORM objects but never touches a session, following the
`state_machine.py` precedent of a `TYPE_CHECKING`-only model import.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from arip.config import RankingSettings
    from arip.db.models import Item

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-source engagement metric — SDS §5.5.1
# ---------------------------------------------------------------------------

ENGAGEMENT_METRIC_BY_SOURCE: dict[str, str] = {
    "huggingface_papers": "upvotes",
    "huggingface_models": "likes",
    "huggingface_spaces": "likes",
    "github_trending": "stars",
}
"""Key within ``items.source_signals`` carrying each source's engagement metric.

``arxiv`` is deliberately absent: the ArXiv API exposes no engagement metric,
so its items score 0.0 for this signal (§5.5.1, TD-016). ``papers_with_code``
is absent because it is DECLARED-but-UNIMPLEMENTED (§5.3.2); it gains an entry
if and when the source is implemented.
"""

TOPIC_MATCH_DIVISOR = 3
"""Diminishing-returns divisor in ``min(matches / 3, 1.0)`` (SDS §5.5)."""


class Scorer:
    """Computes the composite importance score for a batch of items.

    Scoring is batch-scoped by necessity: the engagement signal is a percentile
    within the current run (§5.5), which cannot be computed for one item in
    isolation. There is therefore no single-item public method.

    The scorer is stateless. Construct it once and reuse it, or construct it
    per run — both are equivalent.
    """

    def __init__(self, config: RankingSettings) -> None:
        """
        Args:
            config: The `ranking` block of `AppSettings`. Weights are already
                    validated to sum to 1.0 ± 0.001 by `SignalWeights`.
        """
        self._config = config

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def score_batch(
        self,
        items: Sequence[Item],
        *,
        now: datetime | None = None,
    ) -> list[tuple[float, dict[str, float]]]:
        """Score every item in the batch.

        Args:
            items: The items being ranked in this run. Engagement percentiles
                   are computed within this collection and nothing outside it
                   (§5.5: "within the current batch (not all-time)").
            now: Reference time for the recency signal. Defaults to the current
                 UTC time; supplied explicitly by tests so that an exponential
                 decay can be asserted deterministically.

        Returns:
            One `(score, breakdown)` pair per input item, in input order
            (§5.5). `breakdown` carries the four raw signal values under the
            keys `recency`, `authority`, `engagement` and `topic` — the same
            names as the weights, so a reader can multiply them out by hand.
        """
        reference = now or datetime.utcnow()
        engagement_by_index = self._engagement_percentiles(items)
        weights = self._config.weights

        results: list[tuple[float, dict[str, float]]] = []
        for index, item in enumerate(items):
            breakdown = {
                "recency": self._recency(item, reference),
                "authority": self._authority(item),
                "engagement": engagement_by_index[index],
                "topic": self._topic_relevance(item),
            }
            score = (
                weights.recency * breakdown["recency"]
                + weights.authority * breakdown["authority"]
                + weights.engagement * breakdown["engagement"]
                + weights.topic * breakdown["topic"]
            )
            # Weights are validated to 1.0 ± 0.001, so the sum can exceed 1.0
            # by a rounding margin. §4.2 declares importance_score as [0.0-1.0].
            results.append((_clamp(score), breakdown))

        return results

    # ------------------------------------------------------------------
    # Signal: recency
    # ------------------------------------------------------------------

    def _recency(self, item: Item, reference: datetime) -> float:
        """Exponential decay on item age: `exp(-k × days_elapsed)`.

        A NULL `published_date` scores 0.0 (§5.5.1). A date in the future is
        treated as zero days elapsed, capping the signal at 1.0 rather than
        letting a bad source date score above the maximum.
        """
        published = item.published_date
        if published is None:
            return 0.0

        days_elapsed = (reference.date() - _as_date(published)).days
        if days_elapsed < 0:
            days_elapsed = 0

        return math.exp(-self._config.recency_k * days_elapsed)

    # ------------------------------------------------------------------
    # Signal: source authority
    # ------------------------------------------------------------------

    def _authority(self, item: Item) -> float:
        """Static per-source weight from `config.ranking.source_authority`.

        A `source_id` absent from the mapping scores 0.0 and is logged. The
        mapping covers every declared source, so this indicates a source added
        without a corresponding config entry.
        """
        authority = self._config.source_authority.get(item.source_id)
        if authority is None:
            logger.warning("authority_unknown_source", source_id=item.source_id)
            return 0.0
        return authority

    # ------------------------------------------------------------------
    # Signal: engagement
    # ------------------------------------------------------------------

    def _engagement_percentiles(self, items: Sequence[Item]) -> list[float]:
        """Compute the engagement signal for every item in the batch.

        Items are grouped into cohorts by `source_type` (§5.5.1), and each
        item's score is its metric's percentile within its cohort.

        Items whose source has no engagement metric — or whose metric cannot be
        read — score 0.0 and are excluded from cohort statistics entirely. They
        are not counted as zeros: doing so would let a metric-less source
        inflate the percentiles of every other source sharing its type.

        Returns:
            One value per input item, in input order.
        """
        raw: list[float | None] = [self._engagement_metric(item) for item in items]

        cohorts: dict[str, list[float]] = {}
        for item, value in zip(items, raw, strict=True):
            if value is not None:
                cohorts.setdefault(item.source_type, []).append(value)

        scores: list[float] = []
        for item, value in zip(items, raw, strict=True):
            if value is None:
                scores.append(0.0)
            else:
                scores.append(_percentile(value, cohorts[item.source_type]))
        return scores

    def _engagement_metric(self, item: Item) -> float | None:
        """Read one item's raw engagement metric.

        Returns:
            The metric value, or None when this source has no engagement
            metric, the item carries no `source_signals`, the JSON is
            unparseable, the key is absent, or the value is not numeric.
            None means "excluded from the cohort and scored 0.0" — it is
            never confused with a genuine zero metric.
        """
        key = ENGAGEMENT_METRIC_BY_SOURCE.get(item.source_id)
        if key is None:
            # Source exposes no engagement metric (e.g. arxiv). Not an error.
            return None

        if not item.source_signals:
            return None

        try:
            signals = json.loads(item.source_signals)
        except (ValueError, TypeError):
            # SDS §5.5: malformed source_signals -> log warning, use 0.0.
            logger.warning(
                "source_signals_unparseable",
                item_id=item.id,
                source_id=item.source_id,
            )
            return None

        if not isinstance(signals, dict):
            logger.warning(
                "source_signals_not_an_object",
                item_id=item.id,
                source_id=item.source_id,
            )
            return None

        value = signals.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            # bool is a subclass of int; a boolean metric is malformed data.
            return None

        return float(value)

    # ------------------------------------------------------------------
    # Signal: topic relevance
    # ------------------------------------------------------------------

    def _topic_relevance(self, item: Item) -> float:
        """`min(matches / 3, 1.0)` over the configured topic keywords.

        A keyword matches when it appears, lowercased, as a substring of the
        item's title, abstract and topics concatenated (§5.5.1). Each keyword
        counts at most once no matter how often it occurs.
        """
        keywords = self._config.topic_keywords
        if not keywords:
            return 0.0

        haystack = self._match_text(item)
        matches = sum(1 for keyword in keywords if keyword.lower() in haystack)
        return min(matches / TOPIC_MATCH_DIVISOR, 1.0)

    def _match_text(self, item: Item) -> str:
        """Build the lowercase text that topic keywords are matched against.

        `title + " " + (abstract or "") + " " + " ".join(topics or [])`,
        per §5.5.1. `topics` is a JSON list on the item; unparseable JSON is
        treated as no topics and logged, mirroring the `source_signals` rule.
        """
        parts = [item.title or "", item.abstract or ""]

        if item.topics:
            try:
                topics = json.loads(item.topics)
            except (ValueError, TypeError):
                logger.warning("topics_unparseable", item_id=item.id)
                topics = None
            if isinstance(topics, list):
                parts.extend(str(topic) for topic in topics)

        return " ".join(parts).lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(value: float, cohort: Sequence[float]) -> float:
    """Percentile rank of `value` within `cohort`, in [0.0, 1.0].

    Defined as the fraction of cohort members strictly below `value`, so the
    cohort maximum scores 1.0 and the minimum scores 0.0.

    A cohort of one scores 1.0 (§5.5.1: "Batch minimum = 1 if only one item" —
    the sole member is trivially its own maximum).

    A cohort whose members all share one value scores 0.0 for all of them:
    no member is above any other, so none earns credit.
    """
    if len(cohort) <= 1:
        return 1.0

    below = sum(1 for other in cohort if other < value)
    return below / (len(cohort) - 1)


def _as_date(value: date | datetime) -> date:
    """Return a `date` from either a `date` or a `datetime`.

    `items.published_date` is a DATE column, but SQLite returns whatever was
    stored and tests may construct either.
    """
    if isinstance(value, datetime):
        return value.date()
    return value


def _clamp(value: float) -> float:
    """Bound a score to [0.0, 1.0] as declared for `importance_score` (§4.2)."""
    return max(0.0, min(1.0, value))
