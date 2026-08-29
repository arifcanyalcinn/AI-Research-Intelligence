"""
Unit tests for Scorer.

Covers the four CPU-only signals of SDS §5.5 with the operational values fixed
by §5.5.1, and the decay constant from §5.15.1.

§5.5's testing strategy applies directly: "Unit test with synthetic items.
Assert weight validation (must sum to 1.0 ± 0.001). Test threshold filtering.
Fully deterministic; no mocking needed."

No database, no session, no network. `Item` objects are constructed in memory —
the scorer reads attributes and never touches a session. `now` is supplied
explicitly to every call so that the exponential decay is asserted against
fixed values rather than wall-clock time.

Threshold filtering itself is not tested here: the scorer computes scores and
does not decide anything. `RankStage` applies `min_score` and owns that test
(§8.5).
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from arip.config import RankingSettings, SignalWeights
from arip.db.models import Item
from arip.ranking.scorer import ENGAGEMENT_METRIC_BY_SOURCE, Scorer

NOW = datetime(2026, 8, 29, 12, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_item(
    source_id: str = "arxiv",
    source_type: str = "PAPER",
    *,
    published_date: date | None = None,
    title: str | None = "A Paper About Things",
    abstract: str | None = None,
    topics: list[str] | None = None,
    source_signals: dict | None = None,
    raw_topics: str | None = None,
    raw_signals: str | None = None,
    item_id: int = 1,
) -> Item:
    """Build an in-memory Item with the columns the scorer reads.

    `raw_topics` / `raw_signals` bypass JSON encoding so malformed column
    content can be exercised.
    """
    return Item(
        id=item_id,
        source_id=source_id,
        source_type=source_type,
        external_id=f"ext-{item_id}",
        content_hash=f"hash-{item_id}",
        status="COLLECTED",
        language="EN",
        title=title,
        abstract=abstract,
        published_date=published_date,
        topics=raw_topics if raw_topics is not None else _dump(topics),
        source_signals=raw_signals if raw_signals is not None else _dump(source_signals),
    )


def _dump(value: object | None) -> str | None:
    return None if value is None else json.dumps(value)


def settings(**overrides: object) -> RankingSettings:
    """RankingSettings with test-friendly defaults."""
    base: dict[str, object] = {
        "min_score": 0.35,
        "recency_k": 0.15,
        "weights": SignalWeights(),
        "topic_keywords": [],
        "source_authority": {
            "arxiv": 0.85,
            "huggingface_papers": 0.80,
            "github_trending": 0.65,
            "huggingface_models": 0.55,
            "huggingface_spaces": 0.45,
        },
    }
    base.update(overrides)
    return RankingSettings(**base)  # type: ignore[arg-type]


def only_signal(name: str) -> SignalWeights:
    """Weights placing the entire mass on one signal, to isolate it."""
    values = {"recency": 0.0, "authority": 0.0, "engagement": 0.0, "topic": 0.0}
    values[name] = 1.0
    return SignalWeights(**values)  # type: ignore[arg-type]


def score_one(item: Item, config: RankingSettings) -> tuple[float, dict[str, float]]:
    return Scorer(config).score_batch([item], now=NOW)[0]


# ---------------------------------------------------------------------------
# Recency — SDS §5.5, §5.5.1, §5.15.1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("days", [0, 1, 3, 7, 30])
def test_recency_follows_exponential_decay(days: int) -> None:
    """recency = exp(-k x days_elapsed)."""
    item = make_item(published_date=(NOW.date() - timedelta(days=days)))

    _, breakdown = score_one(item, settings())

    assert breakdown["recency"] == pytest.approx(math.exp(-0.15 * days))


def test_recency_is_one_on_the_publication_day() -> None:
    """Zero days elapsed gives the maximum recency."""
    item = make_item(published_date=NOW.date())

    _, breakdown = score_one(item, settings())

    assert breakdown["recency"] == pytest.approx(1.0)


def test_recency_is_zero_when_published_date_is_null() -> None:
    """Missing data earns no credit (SDS §5.5.1)."""
    item = make_item(published_date=None)

    _, breakdown = score_one(item, settings())

    assert breakdown["recency"] == 0.0


def test_future_publication_date_does_not_exceed_one() -> None:
    """A bad source date cannot push the signal above its maximum."""
    item = make_item(published_date=date(2027, 1, 1))

    _, breakdown = score_one(item, settings())

    assert breakdown["recency"] == pytest.approx(1.0)


def test_recency_k_is_read_from_config() -> None:
    """The decay constant is configuration, not a constant (SDS §5.15.1)."""
    item = make_item(published_date=date(2026, 8, 19))  # 10 days before NOW

    _, slow = score_one(item, settings(recency_k=0.05))
    _, fast = score_one(item, settings(recency_k=0.50))

    assert slow["recency"] == pytest.approx(math.exp(-0.5))
    assert fast["recency"] == pytest.approx(math.exp(-5.0))
    assert fast["recency"] < slow["recency"]


def test_recency_accepts_a_datetime_published_date() -> None:
    """SQLite may hand back a datetime where a date was stored."""
    item = make_item(published_date=datetime(2026, 8, 26, 9, 30))

    _, breakdown = score_one(item, settings())

    assert breakdown["recency"] == pytest.approx(math.exp(-0.15 * 3))


# ---------------------------------------------------------------------------
# Source authority — SDS §5.5
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_id", "expected"),
    [
        ("arxiv", 0.85),
        ("huggingface_papers", 0.80),
        ("github_trending", 0.65),
        ("huggingface_models", 0.55),
        ("huggingface_spaces", 0.45),
    ],
)
def test_authority_comes_from_config(source_id: str, expected: float) -> None:
    """Each source's authority is the static weight configured for it."""
    item = make_item(source_id=source_id)

    _, breakdown = score_one(item, settings())

    assert breakdown["authority"] == expected


def test_unknown_source_scores_zero_authority() -> None:
    """A source with no configured authority earns none."""
    item = make_item(source_id="not_a_configured_source")

    _, breakdown = score_one(item, settings())

    assert breakdown["authority"] == 0.0


def test_authority_honours_a_reconfigured_value() -> None:
    """Authority is configuration, not a constant."""
    item = make_item(source_id="arxiv")

    _, breakdown = score_one(item, settings(source_authority={"arxiv": 0.10}))

    assert breakdown["authority"] == 0.10


# ---------------------------------------------------------------------------
# Engagement — SDS §5.5, §5.5.1
# ---------------------------------------------------------------------------


def test_engagement_metric_map_matches_the_amendment() -> None:
    """The per-source metric keys are exactly those fixed by SDS §5.5.1."""
    assert ENGAGEMENT_METRIC_BY_SOURCE == {
        "huggingface_papers": "upvotes",
        "huggingface_models": "likes",
        "huggingface_spaces": "likes",
        "github_trending": "stars",
    }


def test_arxiv_has_no_engagement_metric() -> None:
    """ArXiv exposes none, so it is absent from the map (TD-016)."""
    assert "arxiv" not in ENGAGEMENT_METRIC_BY_SOURCE


def test_models_use_likes_not_downloads() -> None:
    """huggingface_models resolves to likes; downloads is present and unused."""
    high_downloads = make_item(
        "huggingface_models", "MODEL",
        source_signals={"downloads": 1_000_000, "likes": 1},
        item_id=1,
    )
    high_likes = make_item(
        "huggingface_models", "MODEL",
        source_signals={"downloads": 1, "likes": 500},
        item_id=2,
    )

    results = Scorer(settings()).score_batch([high_downloads, high_likes], now=NOW)

    assert results[0][1]["engagement"] == 0.0
    assert results[1][1]["engagement"] == 1.0


def test_engagement_is_a_percentile_within_the_cohort() -> None:
    """The cohort maximum scores 1.0 and the minimum 0.0."""
    items = [
        make_item("github_trending", "REPO", source_signals={"stars": n}, item_id=n)
        for n in (10, 20, 30)
    ]

    results = Scorer(settings()).score_batch(items, now=NOW)

    assert [r[1]["engagement"] for r in results] == [0.0, 0.5, 1.0]


def test_single_item_cohort_scores_one() -> None:
    """SDS §5.5.1: the sole member of a cohort is trivially its maximum."""
    item = make_item("github_trending", "REPO", source_signals={"stars": 0})

    _, breakdown = score_one(item, settings())

    assert breakdown["engagement"] == 1.0


def test_cohort_with_identical_values_scores_zero() -> None:
    """No member is above any other, so none earns credit.

    Contrast with the single-item case above: §5.5.1 fixes n=1 at 1.0, and the
    percentile definition gives 0.0 when every member ties. Both are
    deliberate; see the Stage 1 delivery note.
    """
    items = [
        make_item("github_trending", "REPO", source_signals={"stars": 7}, item_id=n)
        for n in (1, 2, 3)
    ]

    results = Scorer(settings()).score_batch(items, now=NOW)

    assert [r[1]["engagement"] for r in results] == [0.0, 0.0, 0.0]


def test_cohorts_are_separated_by_source_type() -> None:
    """A repo's stars are never ranked against a space's likes."""
    repo_low = make_item("github_trending", "REPO", source_signals={"stars": 1}, item_id=1)
    repo_high = make_item("github_trending", "REPO", source_signals={"stars": 9}, item_id=2)
    space = make_item("huggingface_spaces", "SPACE", source_signals={"likes": 5}, item_id=3)

    results = Scorer(settings()).score_batch([repo_low, repo_high, space], now=NOW)

    assert results[0][1]["engagement"] == 0.0
    assert results[1][1]["engagement"] == 1.0
    # Sole member of the SPACE cohort.
    assert results[2][1]["engagement"] == 1.0


def test_metricless_source_is_excluded_from_cohort_statistics() -> None:
    """ArXiv items must not inflate the percentiles of other PAPER sources.

    Both hf_papers items share the PAPER source_type with the arxiv item. If
    arxiv were counted as a zero, the lower hf_papers item would rank above it
    and score 0.5 instead of 0.0.
    """
    arxiv = make_item("arxiv", "PAPER", item_id=1)
    low = make_item("huggingface_papers", "PAPER", source_signals={"upvotes": 2}, item_id=2)
    high = make_item("huggingface_papers", "PAPER", source_signals={"upvotes": 8}, item_id=3)

    results = Scorer(settings()).score_batch([arxiv, low, high], now=NOW)

    assert results[0][1]["engagement"] == 0.0
    assert results[1][1]["engagement"] == 0.0
    assert results[2][1]["engagement"] == 1.0


def test_absent_source_signals_scores_zero() -> None:
    """A source with a metric but no signals column earns nothing."""
    item = make_item("github_trending", "REPO", source_signals=None)

    _, breakdown = score_one(item, settings())

    assert breakdown["engagement"] == 0.0


def test_missing_metric_key_scores_zero() -> None:
    """Signals present but without the source's key earns nothing."""
    item = make_item("github_trending", "REPO", source_signals={"forks": 12})

    _, breakdown = score_one(item, settings())

    assert breakdown["engagement"] == 0.0


def test_malformed_source_signals_scores_zero() -> None:
    """SDS §5.5: malformed source_signals -> warning, 0.0. Never raises."""
    item = make_item("github_trending", "REPO", raw_signals="{not valid json")

    _, breakdown = score_one(item, settings())

    assert breakdown["engagement"] == 0.0


def test_source_signals_that_is_not_an_object_scores_zero() -> None:
    """Valid JSON of the wrong shape is still unusable."""
    item = make_item("github_trending", "REPO", raw_signals="[1, 2, 3]")

    _, breakdown = score_one(item, settings())

    assert breakdown["engagement"] == 0.0


def test_non_numeric_metric_scores_zero() -> None:
    """A string where a count belongs is malformed data."""
    item = make_item("github_trending", "REPO", source_signals={"stars": "many"})

    _, breakdown = score_one(item, settings())

    assert breakdown["engagement"] == 0.0


def test_boolean_metric_scores_zero() -> None:
    """bool subclasses int; a boolean metric must not be read as 0 or 1."""
    item = make_item("github_trending", "REPO", source_signals={"stars": True})

    _, breakdown = score_one(item, settings())

    assert breakdown["engagement"] == 0.0


def test_zero_metric_is_a_real_value_not_missing_data() -> None:
    """An item with 0 stars still participates in its cohort."""
    zero = make_item("github_trending", "REPO", source_signals={"stars": 0}, item_id=1)
    ten = make_item("github_trending", "REPO", source_signals={"stars": 10}, item_id=2)

    results = Scorer(settings()).score_batch([zero, ten], now=NOW)

    assert results[0][1]["engagement"] == 0.0
    assert results[1][1]["engagement"] == 1.0


# ---------------------------------------------------------------------------
# Topic relevance — SDS §5.5, §5.5.1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Nothing relevant here", 0.0),
        ("A study of the transformer", 1 / 3),
        ("transformer and diffusion", 2 / 3),
        ("transformer diffusion reasoning", 1.0),
        ("transformer diffusion reasoning agent LLM", 1.0),
    ],
)
def test_topic_score_is_matches_over_three_capped_at_one(title: str, expected: float) -> None:
    """min(matches / 3, 1.0), per SDS §5.5."""
    config = settings(
        topic_keywords=["transformer", "diffusion", "reasoning", "agent", "LLM"],
        weights=only_signal("topic"),
    )
    item = make_item(title=title)

    score, breakdown = score_one(item, config)

    assert breakdown["topic"] == pytest.approx(expected)
    assert score == pytest.approx(expected)


def test_topic_matching_is_case_insensitive() -> None:
    """Keywords are lowercased before matching (SDS §5.5.1)."""
    config = settings(topic_keywords=["LLM"])
    item = make_item(title="Scaling llm Inference")

    _, breakdown = score_one(item, config)

    assert breakdown["topic"] == pytest.approx(1 / 3)


def test_topic_matching_is_substring_not_whole_token() -> None:
    """'fine-tuning' matches inside 'fine-tuned' (SDS §5.5.1)."""
    config = settings(topic_keywords=["fine-tun"])
    item = make_item(title="A fine-tuned model")

    _, breakdown = score_one(item, config)

    assert breakdown["topic"] == pytest.approx(1 / 3)


def test_topic_matches_against_the_abstract() -> None:
    """The abstract is part of the matched text."""
    config = settings(topic_keywords=["reinforcement learning"])
    item = make_item(title="Untitled", abstract="We apply reinforcement learning to robots.")

    _, breakdown = score_one(item, config)

    assert breakdown["topic"] == pytest.approx(1 / 3)


def test_topic_matches_against_the_topics_list() -> None:
    """Topics are part of the matched text."""
    config = settings(topic_keywords=["cs.ai"])
    item = make_item(title="Untitled", topics=["cs.AI", "cs.LG"])

    _, breakdown = score_one(item, config)

    assert breakdown["topic"] == pytest.approx(1 / 3)


def test_each_keyword_counts_once_however_often_it_appears() -> None:
    """Repetition does not inflate the score."""
    config = settings(topic_keywords=["agent"])
    item = make_item(title="agent agent agent agent agent")

    _, breakdown = score_one(item, config)

    assert breakdown["topic"] == pytest.approx(1 / 3)


def test_empty_keyword_list_scores_zero() -> None:
    """With nothing configured to match, nothing matches."""
    item = make_item(title="transformer diffusion reasoning")

    _, breakdown = score_one(item, settings(topic_keywords=[]))

    assert breakdown["topic"] == 0.0


def test_malformed_topics_json_does_not_raise() -> None:
    """Unparseable topics are treated as absent; title still matches."""
    config = settings(topic_keywords=["transformer"])
    item = make_item(title="A transformer study", raw_topics="{broken")

    _, breakdown = score_one(item, config)

    assert breakdown["topic"] == pytest.approx(1 / 3)


def test_item_with_no_text_at_all_scores_zero() -> None:
    """Title, abstract and topics all absent."""
    config = settings(topic_keywords=["transformer"])
    item = make_item(title=None, abstract=None, topics=None)

    _, breakdown = score_one(item, config)

    assert breakdown["topic"] == 0.0


# ---------------------------------------------------------------------------
# Composite score — SDS §5.5, §4.2
# ---------------------------------------------------------------------------


def test_composite_is_the_weighted_sum_of_the_four_signals() -> None:
    """The score is reproducible by hand from the breakdown and the weights."""
    item = make_item(
        "github_trending", "REPO",
        published_date=NOW.date(),
        title="A transformer study",
        source_signals={"stars": 5},
    )
    config = settings(topic_keywords=["transformer"])

    score, breakdown = score_one(item, config)

    expected = (
        0.25 * breakdown["recency"]
        + 0.25 * breakdown["authority"]
        + 0.35 * breakdown["engagement"]
        + 0.15 * breakdown["topic"]
    )
    assert score == pytest.approx(expected)


def test_breakdown_keys_match_the_weight_names() -> None:
    """A reader can multiply the breakdown out against the weights."""
    _, breakdown = score_one(make_item(), settings())

    assert set(breakdown) == {"recency", "authority", "engagement", "topic"}


def test_breakdown_carries_no_novelty_signal() -> None:
    """AD-19: novelty is not a ranking signal."""
    _, breakdown = score_one(make_item(), settings())

    assert "novelty" not in breakdown


def test_score_is_bounded_between_zero_and_one() -> None:
    """§4.2 declares importance_score as [0.0-1.0]."""
    best = make_item(
        "github_trending", "REPO",
        published_date=NOW.date(),
        title="transformer diffusion reasoning",
        source_signals={"stars": 100},
    )
    worst = make_item("not_configured", "PAPER", published_date=None, title=None, item_id=2)
    config = settings(topic_keywords=["transformer", "diffusion", "reasoning"])

    results = Scorer(config).score_batch([best, worst], now=NOW)

    for score, _ in results:
        assert 0.0 <= score <= 1.0


def test_worst_possible_item_scores_zero() -> None:
    """No date, no authority, no engagement, no topic match."""
    item = make_item("not_configured", "PAPER", published_date=None, title=None)

    score, _ = score_one(item, settings())

    assert score == 0.0


def test_results_are_returned_in_input_order() -> None:
    """The caller maps results back to items positionally."""
    items = [
        make_item("github_trending", "REPO", source_signals={"stars": n}, item_id=n)
        for n in (5, 1, 3)
    ]

    results = Scorer(settings()).score_batch(items, now=NOW)

    assert [r[1]["engagement"] for r in results] == [1.0, 0.0, 0.5]


def test_empty_batch_returns_empty_list() -> None:
    """Nothing to score is not an error."""
    assert Scorer(settings()).score_batch([], now=NOW) == []


def test_scoring_is_deterministic() -> None:
    """Same inputs, same outputs — no randomness anywhere (SDS §5.5)."""
    items = [
        make_item("github_trending", "REPO", published_date=NOW.date(),
                  source_signals={"stars": n}, item_id=n)
        for n in (1, 2, 3)
    ]
    scorer = Scorer(settings())

    first = scorer.score_batch(items, now=NOW)
    second = scorer.score_batch(items, now=NOW)

    assert first == second


def test_scoring_does_not_mutate_the_items() -> None:
    """The scorer computes; RankStage decides and writes."""
    item = make_item("github_trending", "REPO", source_signals={"stars": 5})

    score_one(item, settings())

    assert item.importance_score is None
    assert item.signal_breakdown is None
    assert item.status == "COLLECTED"


# ---------------------------------------------------------------------------
# Weight validation — SDS §5.5 testing strategy, §5.15
# ---------------------------------------------------------------------------


def test_weights_that_do_not_sum_to_one_are_rejected() -> None:
    """Asserts the existing config-level guard, per §5.5's testing strategy."""
    with pytest.raises(ValidationError):
        SignalWeights(recency=0.5, authority=0.5, engagement=0.5, topic=0.5)


def test_weights_within_tolerance_are_accepted() -> None:
    """The tolerance is 1.0 +/- 0.001."""
    weights = SignalWeights(recency=0.25, authority=0.25, engagement=0.35, topic=0.1505)

    assert weights.topic == 0.1505


def test_reweighting_changes_the_composite() -> None:
    """Weights are configuration, and the scorer honours them."""
    item = make_item(
        "github_trending", "REPO",
        published_date=NOW.date(),
        source_signals={"stars": 5},
    )

    recency_only, _ = score_one(item, settings(weights=only_signal("recency")))
    authority_only, _ = score_one(item, settings(weights=only_signal("authority")))

    assert recency_only == pytest.approx(1.0)
    assert authority_only == pytest.approx(0.65)


# ---------------------------------------------------------------------------
# ArXiv ageing — TD-016, asserted rather than described
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected_score", "passes_threshold"),
    [
        (0, 0.4625, True),
        (3, 0.3719, True),
        (5, 0.3306, False),
        (7, 0.3000, False),
    ],
)
def test_arxiv_ages_below_threshold_without_engagement(
    days: int, expected_score: float, passes_threshold: bool
) -> None:
    """TD-016 as an executable assertion.

    ArXiv carries the highest authority (0.85) and no engagement metric, so its
    score decays on age alone. This test pins the numbers so that a future
    change to the weights, to recency_k, or to min_score surfaces here rather
    than in a live run.
    """
    config = settings()
    item = make_item(
        "arxiv", "PAPER",
        published_date=NOW.date() - timedelta(days=days),
        title="Untitled",
    )

    score, _ = score_one(item, config)

    assert score == pytest.approx(expected_score, abs=0.0005)
    assert (score >= config.min_score) is passes_threshold


def test_raising_recency_k_ages_arxiv_out_faster() -> None:
    """§5.15.1's stated purpose: recency_k is the operator's lever (TD-016)."""
    item = make_item(
        "arxiv", "PAPER",
        published_date=NOW.date() - timedelta(days=3),
        title="Untitled",
    )

    default, _ = score_one(item, settings(recency_k=0.15))
    aggressive, _ = score_one(item, settings(recency_k=0.60))

    assert default >= 0.35
    assert aggressive < 0.35


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------


def test_recency_k_defaults_to_the_documented_value() -> None:
    """SDS §5.15.1 declares recency_k: float = 0.15."""
    assert RankingSettings().recency_k == 0.15
