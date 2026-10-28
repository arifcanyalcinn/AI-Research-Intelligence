# Batch 10 — Decisions Record

**Scope:** Ranking calibration, reduced scope: cleanup (TD-002, TD-006, TD-008, TD-014), TD-016
with TD-017 assessed, the re-scoring path for the existing pool, and the GitHub AI filter
(analysis only).

**Status at time of writing:** Complete. Stages 1, 1b, 2, 3 and 4 applied; live run measured;
criteria S1–S9 met.

**What this file is.** A record of every ruling made during Batch 10 and the reason for it, so a
later reader can act on them without reconstructing the conversation. It is not the completion
report.

**What this file is not.** It is not an amendment to the SDS and has no authority over it. The
one amendment made in this batch is SDS §5.5.2; `docs/frozen_sds.md` remains the single source
of truth.

---

## A1 — The §5.5.1 sentence covers ArXiv: Reading A

§5.5.1 says a source whose `source_signals` is "absent, NULL, or missing its key scores 0.0", and
its own table marks `arxiv` *(none)* with `source_signals` NULL. §5.15.1 shows the author knew a
metric-less source ages out on recency. **Ruling:** 0.0 for ArXiv was SDS-mandated, so changing
it required an amendment. Done as §5.5.2, in the §3.5 / §5.15.1 format, with the original text
left in place.

## A2 — An explicit named set, not "absent from the metric map"

The scorer's "no metric" branch fired for any `source_id` missing from
`ENGAGEMENT_METRIC_BY_SOURCE`, which included unknown and misconfigured sources. **Ruling:** the
declared value goes only to `METRICLESS_SOURCES = {"arxiv"}`, a module constant. Unknown or
unmapped sources and per-item defects keep 0.0. Pinned by `test_worst_possible_item_scores_zero`
and the six defect tests, and by mutations M2 and M3.

## A3 — Option (c), a declared value; then R1: 0.35 → 0.24

Three options were measured: (a) 0.0; (b) drop the signal and renormalise; (c) a declared value.
(b) was rejected: it makes the configured weights differ from the applied ones, gives arxiv a
different weight vector, and breaks "breakdown × weights = score".

The value was first ruled 0.35. The Stage 3 pre-code showed that at 0.35 an arxiv item with one
or more topic matches has a floor of 0.385, above `min_score`, and never ages out. **R1:** value
**0.24**. It gives the same top 50 on the measured pool as 0.35 (13 / 30 / 7) and keeps age-out
for items with at most one topic match (67% of the pool, against 41% at 0.35). 0.24 is the
largest two-decimal value that does so: at 0.25 the one-match floor equals `min_score`, which
mutation M4 confirms.

## A4 — TD-017: no behaviour change

The single-member value follows from §5.5's own text ("Batch minimum = 1 if only one item").
Changing the tie rule would alter engagement for every source that publishes a metric. §5.5.2
records the asymmetry in a "considered, not changed" paragraph.

## A5 — Cohort key: no action

Cohorts are keyed by `source_type` per §5.5.1's derived point. While arxiv is excluded, every
metric-bearing source is alone in its type, so no test can distinguish the two keys today. It
becomes live only if `papers_with_code` is implemented with a metric.

## A6 — Re-scoring by fresh database, not in place

No state returns to COLLECTED from ENRICHED. The ENRICHED → FAILED → COLLECTED path is
mechanically allowed by `VALID_TRANSITIONS` but forbidden by §3.3, and **must not be used**.
**Ruling:** archive `arip.db`, `-wal`, `-shm` and the ANN index together (a stale index with a
fresh database would produce silent false duplicates); create a fresh database; one live run,
started by the user after explicit approval. Before/after comparison is read-only: same items
compared only within one database; old against new only as statistics.

## A7 — GitHub AI filter deferred (TD-022)

The quoted "SDS §5.3.1" acquisition rule does not exist in the frozen SDS; the acquisition
approach is a Batch 5 project decision. Live check by the user, 2026-10-25:
`topic:a OR topic:b` returns HTTP 422 ("Logical operators only apply to text, not to
qualifiers"); keyword OR works. Stage 1 corrected the false citations, docstrings only.

## A8 — `ruff format` adopted (Stage 1b); D-007; A8b

Adopted as a standalone stage before any logic change, proved by syntax-tree comparison. The gate
is extended by **D-007** ("every completed batch must also pass `ruff format --check .`").
**A8b:** "Decision 001–005" stays as the historical register; new decisions continue at D-007;
TD-006's citation now reads "Decision 004 (historical register)".

## A9 — TD-014: documentation only

README "Setup" section. Whether the CLI may create directories or run migrations is not
specified by the SDS; a startup check belongs to Phase 7's `RecoveryService`.

## A10 — Logging

Reported only. §5.5.2 states the current behaviour: nothing is logged for a metric-less source;
for per-item defects a warning is logged when `source_signals` cannot be parsed as a JSON object.

---

## Stage rulings

**Stage 1.** The project log and debt register are not edited by the implementer: debt moves go
into the end-of-batch block (Q1). Two `mkdir` lines instead of `mkdir data logs`, because
PowerShell behaviour is unverified (Q2). Correct `§5.3.1` references at test lines 176 and 471
are kept (Q3). The setup note goes in README.md (Q4).

**Stage 1b.** U1: the 7 adjacent-literal pairs are merged by hand before formatting (Python joins
adjacent literals when parsing, so the syntax tree is identical). U2/U3: alignment and
argument-splitting changes accepted. The proof script is delivered outside the repository (Q2).
D-007 worded "every completed batch".

**Stage 2.** New file `tools/measure_ranking_pool.py`; `measure_signals.py` unchanged (Q1). A
`snapshot` subcommand using SQLite's backup API, with the manual three-file copy documented as
the fallback (Q2): a database-only copy taken while the WAL holds committed data silently omits
that data. Top-20 GitHub topics included (Q3). Check script delivered outside the repository
(Q4).

**Stage 3.** R1 as in A3. R2: `test_arxiv_ages_below_threshold_without_engagement` renamed to
`…_on_recency_alone`. R3: the implementer delivers the edited `frozen_sds.md`; the insertion must
show as additions only. Commit 1 (test split, no behaviour change) is separate from commit 2.

**Stage 4.** Archive at `%USERPROFILE%\arip-archive\batch10-before-2026-10-28` (Q1). Stored
baseline, entered/left counts and `pipeline_runs` counts kept (Q2). Added to the procedure: an
`ARIP_*` pre-flight check before archiving, before the migration and before the run; a
self-contained rollback block; STOP points before the run and at the end; a note that the
embedding model may need re-downloading.
