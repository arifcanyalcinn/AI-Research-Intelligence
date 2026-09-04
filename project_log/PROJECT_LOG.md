# Project Log

## Current Status

Current Phase: Phase 1
Current Batch: Batch 8 (Completed)
Next Batch: Batch 9 (embeddings and semantic deduplication)
Architecture: Frozen (Frozen_SDS.md)

---

## Completed Batches

### Batch 1
Infrastructure

Summary

- Source HTTP client abstraction
- Source registry infrastructure
- Plugin discovery
- Shared retry / timeout utilities

Validation

- Tests passed
- Ruff passed

---

### Batch 2
ArXiv Source

Summary

- ArXivSource implemented
- Atom parser
- Retry logic
- Health check
- Registry integration

Validation

- Pytest: 157 passed
- Ruff: PASS

---

## Architecture Decisions

- Synchronous pipeline
- Explicit plugin imports
- SQLite
- Telegram review stage
- Twitter publisher
- Shared build_client()
- Shared retry policy

---

## Technical Debt

Tracked separately in `project_log/TECHNICAL_DEBT.MD`.

---

## Current State

Completed

- Batch 1
- Batch 2
- Batch 3
- Batch 4
- Batch 5

Deferred

- Batch 6 (Papers With Code — no public API available)

Next

- Batch 9
- Embeddings and semantic deduplication (remainder of SDS Phase 3)

---

## Notes

Always verify:

- Ruff
- Pytest
- SDS Compliance Table
- Architecture Review
- Test Review

---

# Batch 3 – HuggingFace Papers Source

## Summary

Implemented the HuggingFace Papers source plugin according to the Frozen SDS.

## Implemented

* Added `HuggingFacePapersSource`
* Implemented Daily Papers API integration
* Reused shared HTTP client (`build_client()`)
* Reused shared retry policy (`FETCH_RETRY`)
* Added payload normalization
* Added `health_check()`
* Registered plugin via `arip/sources/__init__.py`
* Added comprehensive unit tests

## Validation

* SDS compliance reviewed
* Architecture review completed
* Pytest: **218 / 218 passed**
* No architecture violations detected

## Git

Commit:

`a39562c`

Message:

`feat(batch3): add HuggingFace Papers source plugin`

## Next



# Batch 4 – HuggingFace Models & Spaces Sources

## Summary

Implemented the HuggingFace Models and HuggingFace Spaces source plugins
according to the Frozen SDS. Both are independent, direct `BaseSource`
subclasses reusing the Batch 1 infrastructure without modification.

## Implemented

* Added `HuggingFaceModelsSource` (`SourceType.MODEL`, authority 0.55)
* Added `HuggingFaceSpacesSource` (`SourceType.SPACE`, authority 0.45)
* Models API: `GET /api/models?sort=downloads&direction=-1&limit=50`
* Spaces API: `GET /api/spaces?sort=likes&direction=-1&limit=50`
* Reused shared HTTP client (`build_client()`)
* Reused shared retry policy (`FETCH_RETRY`)
* Reused shared hash helper (`compute_content_hash()`)
* Added payload normalization + tag→topic filtering
* Added `health_check()` to both
* Registered both plugins via `arip/sources/__init__.py`
* Added comprehensive unit tests (fixtures captured from live API responses)

## Decisions

* D-004 — Source plugins must inherit `BaseSource` directly (no intermediate
  base class), because `__subclasses__()` does not recurse.
* D-005 — Per-source fetch limit / sort key are module constants when the SDS
  types the source as plain `SourceConfig`.

## SDS References

* §1.2  — explicit plugin registration
* §4.2  — `source_type` MODEL / SPACE, `content_hash` formula
* §5.3  — `BaseSource` interface, retry / 401 / 429 failure modes
* §5.4  — `normalize()` as a source method
* §5.5  — engagement signal, source authority 0.55 / 0.45
* §5.15 — both sources typed as plain `SourceConfig`
* §6    — mandated file paths

## Files Changed

| File | Reason |
|------|--------|
| `arip/sources/huggingface_models.py` | NEW — Models plugin |
| `arip/sources/huggingface_spaces.py` | NEW — Spaces plugin |
| `tests/unit/sources/test_huggingface_models.py` | NEW — Models tests |
| `tests/unit/sources/test_huggingface_spaces.py` | NEW — Spaces tests |
| `arip/sources/__init__.py` | MODIFIED — 2 registration imports (SDS §1.2) |

No config change required: `SourcesSettings`, `settings.yaml` and
`ranking.source_authority` already declared both sources in Batch 1.

## Technical Debt

See TECHNICAL_DEBT.md — items TD-003, TD-004, TD-005 added by this batch.

## Validation

* SDS compliance reviewed
* Architecture review completed
* Pytest: **363 / 363 passed** (218 pre-existing unchanged + 145 new)
* Ruff: no new errors introduced
* No architecture violations detected

## Git

Commit:

`e7f1c7c`

Message:

`feat(batch4): add HuggingFace Models and Spaces source plugins`

## Next

---

# Batch 5 – GitHub Trending Source

## Summary

Implemented the GitHub Trending source plugin according to the Frozen SDS and the approved Batch 5 scope.

## Implemented

* Added `GitHubTrendingSource`
* Added `arip/sources/github_trending.py`
* Added comprehensive unit tests in `tests/unit/sources/test_github_trending.py`
* Registered `GitHubTrendingSource` via `arip/sources/__init__.py`
* Reused shared HTTP client (`build_client()`)
* Reused shared retry policy (`FETCH_RETRY`)
* Reused shared hash helper (`compute_content_hash()`)
* Reused existing `BaseSource`, `RawSourcePayload`, `NormalizedItem`, `SourceHealth`, `SourceConfig`, and `SourceError` infrastructure
* Implemented `SourceType.REPO`
* Implemented GitHub repository payload normalization
* Implemented `health_check()`
* Preserved the existing explicit plugin discovery architecture
* No new dependency introduced
* No configuration schema changes introduced

## Decisions

* D-006 — Source plugins may read their own optional secret directly from `os.environ` when the existing plugin construction contract cannot provide an application-level secret.
* GitHub Trending uses the GitHub REST Search API as the data source.
* Trending is approximated using repository creation recency and descending star count.
* Per-source implementation constants remain module-level constants under the existing D-005 rule.

## SDS References

* §1.2 — explicit plugin registration
* §4.2 — `REPO` source type, GitHub slug identity, stars signal, content hash
* §5.3 — `BaseSource` interface and HTTP/retry/error handling
* §5.4 — source-level normalization
* §5.5 — GitHub source authority
* §5.15 — `github_trending: SourceConfig` and optional GitHub token
* §6 — mandated source file path
* §8 — Phase 1 source implementation scope

## Files Changed

| File | Reason |
|------|--------|
| `arip/sources/github_trending.py` | NEW — GitHub Trending source plugin |
| `tests/unit/sources/test_github_trending.py` | NEW — GitHub Trending unit tests |
| `arip/sources/__init__.py` | MODIFIED — explicit plugin registration |

## Validation

* Pytest: **450 / 450 passed**
* Batch 5 Ruff check: **PASS**
* Configuration validation: **PASS**
* SDS compliance reviewed
* Architecture review completed
* Test review completed
* No new dependency introduced
* No unrelated files included in the Batch 5 commit

## Git

Commit:

`d2eb48c`

Message:

`feat(batch5): add GitHub Trending source`

## Next

Batch 6

* PapersWithCode Source

Prerequisites:

* Batch 5 committed and pushed
* Working tree clean

---

# Batch 6 – Papers With Code (DEFERRED)

## Decision
Deferred. Old service (paperswithcode.com) closed on 2025-07-24; Successor service
(paperswithcode.co, Hugging Face) does not publish a documented open programmatic interface.
Only machine-readable surfaces: sitemap containing only URLs and
static archive with no interaction metrics (huggingface.co/pwc-archive).

## Result
- `papers_with_code` DEFINED but NOT APPLIED, `enabled: false`
- Number of active resources: 5
- Number of tests unchanged: 450
- Next: Batch 7 (Integration), with 5 resources

## SDS References

* §1.2 — `papers_with_code` remains declared but unregistered
* §5.5 — PwC source authority 0.75 retained in config
* §5.15 — `papers_with_code: SourceConfig` retained, `enabled: false`
* §8 L1494 / L1509 — Phase 2 gates unmeetable until this source returns

## Technical Debt

See TECHNICAL_DEBT.MD — TD-009 added by this decision.

## Git

Commit:

`30f4e20`

Message:

`docs(batch6): defer Papers With Code source; no public API available`

## Next

Batch 7

* Integration
---

# Batch 7 – Integration (Collection Path)

## Summary

Implemented the collection path required to evaluate the Phase 2 quality gate, per the scope
defined by SDS §8.2. Batches 1–6 delivered the source plugins; Batch 7 delivers the pipeline that
drives them and persists their output. This is the first batch in which any source's `normalize()`
output reaches the database.

Phase 1 analysis established that SDS Phase 1 (the walking skeleton) had been skipped entirely —
`PipelineOrchestrator` was a stub and no stage existed — and that "Integration" had never been
defined anywhere. Scope option A2 was approved: orchestrator, collection stage, `pipeline_runs`
concurrency guard, and per-run `SourceHealth` logging. The remaining Phase 1 stages are later
batches.

## Implemented

* Added `RawPayloadRepository` (`arip/db/repositories/raw_payloads.py`) — owner of
  `raw_source_payloads` per SDS §5.14.1
* Added `PipelineRunRepository` (`arip/db/repositories/pipeline_runs.py`) — owner of
  `pipeline_runs`, including the §4.6 startup reconciliation
* Added `CollectStage` (`arip/pipeline/stages/collect.py`) — fetch → normalize → exact
  deduplicate → persist
* Replaced the `PipelineOrchestrator` Phase 0 stub with a working `run_once()`
* Wired `SourceRegistry` and `PipelineOrchestrator` into `container.py`
* Replaced the `__main__.py` Phase 0 stub so `python -m arip run` executes a real run
* Implemented `content_hash` exact deduplication against the database (Phase 2 deliverable —
  the hash existed since Batch 1 but was never checked)
* Implemented per-run `SourceHealth` logging (Phase 2 deliverable — `health_check()` existed on
  all five sources but nothing called it)
* Fixed the stale `ItemRepository.create()` docstring that listed `raw_payload` as a required key
* Forced UTF-8 on stdout and stderr in `__main__.py` — piping `arip run` on Windows raised
  `UnicodeEncodeError` after a successful run, turning a completed pipeline into a non-zero exit
* Added 82 unit tests

## Decisions

Three judgement calls were required that the SDS does not settle. None has a home in the SDS or
the debt register, so they are recorded here.

* **Three session scopes in `run_once()`, not one.** SDS §5.14 wraps each stage in a single
  `session_scope()`, which rolls back on exception. Sharing that scope with the run bookkeeping
  would roll back the row recording that the run failed. `run_once()` therefore uses three scopes:
  open the run, run the stage, close the run. The run row is bookkeeping, not a stage, so §5.14 is
  upheld rather than bent. A side effect is correct: a hard process kill mid-stage leaves the row
  in RUNNING, which is exactly the state the §4.6 guard reconciles.

* **Source health logging lives in the orchestrator, not the registry.** SDS §5.2 lists "Report
  source health for logging" as a `SourceRegistry` responsibility, but §5.2's declared public
  interface contains only `get_active_sources()` and `get_source()` — there is no method for it.
  Adding one would extend a published interface; the orchestrator also holds the `run_id` that
  §5.16 requires on every log line, which the registry does not. Implemented as
  `PipelineOrchestrator._log_source_health`.

* **The §5.2 responsibility-versus-interface contradiction is left unresolved in the SDS.** §5.2
  assigns a responsibility for which it declares no method. Batch 7 works around it rather than
  amending the SDS, because the workaround costs nothing and the amendment would touch a Batch 1
  interface for no functional gain. Recorded here so a future reader does not mistake the
  placement for an oversight.

Additional derived decisions, smaller in scope:

* `CollectStage._is_already_collected()` checks `(source_id, external_id)` in addition to
  `content_hash`, because a source revising an item's title changes the hash and would otherwise
  hit the `UNIQUE(source_id, external_id)` constraint declared in §4.2.
* The normalization-failure path computes `content_hash` with an empty title, since the §4.2
  formula requires one and normalization never produced it. The value stays deterministic and
  unique per `(source_id, external_id)`.
* `CollectStage` does not apply `pipeline.max_items_per_run`; its enforcement point is a §9.2
  deferred decision.
* The §3.3 normalization retry policy is not implemented. Without the §4.7 replay capability an
  item left in COLLECTED is re-fetched and then dedup-skipped, so the literal SDS text produces an
  item that silently never progresses. Deferred in §9.2.

## SDS References

* §3.3 — `COLLECTED → FAILED`, `failed_at_stage='NORMALIZATION'`
* §3.4 / AD-03 — `StateMachine.transition()` as sole status mutator
* §4.2 — `content_hash` formula; UNIQUE constraints; column types
* §4.6 — `pipeline_runs` concurrency guard
* §4.7 — `raw_source_payloads` written once per item
* §5.2 — registry failure policy; source health responsibility
* §5.3 — source failure modes
* §5.14 / §5.14.1 — repositories; session per unit of work
* §5.16 — structured logging with bound context
* §6 — mandated file paths for stages and repositories
* §8.1 — Phase 2 completion with a deferred source
* §8.2 — batch/phase mapping and Batch 7 scope
* §8.3 — corrected Phase 2 gate items
* §9.2 — deferred decisions: `max_items_per_run`, normalization retry

## Files Changed

| File | Reason |
|------|--------|
| `arip/db/repositories/raw_payloads.py` | NEW — `RawPayloadRepository` (§5.14.1) |
| `arip/db/repositories/pipeline_runs.py` | NEW — `PipelineRunRepository` (§5.14.1, §4.6) |
| `arip/pipeline/stages/__init__.py` | NEW — stages package marker (§6) |
| `arip/pipeline/stages/collect.py` | NEW — `CollectStage` (§6, §8.2) |
| `arip/pipeline/orchestrator.py` | MODIFIED — Phase 0 stub replaced with `run_once()` |
| `arip/container.py` | MODIFIED — `SourceRegistry` and `PipelineOrchestrator` wired in |
| `arip/__main__.py` | MODIFIED — Phase 0 stub replaced with a real run; stdout/stderr forced to UTF-8 |
| `arip/db/repositories/items.py` | MODIFIED — stale `create()` docstring corrected (TD-011) |
| `tests/unit/test_new_repositories.py` | NEW — 30 repository tests |
| `tests/unit/pipeline/__init__.py` | NEW — test package marker |
| `tests/unit/pipeline/test_collect.py` | NEW — 31 `CollectStage` tests |
| `tests/unit/pipeline/test_orchestrator.py` | NEW — 21 orchestrator tests |

No configuration schema change. No new dependency. No source plugin modified.

## Technical Debt

No new technical debt introduced.

Newly exposed and recorded separately: TD-013 (`NormalizedItem` types do not mirror column types;
`_item_columns()` is the only translation point), TD-014 (fresh-checkout bootstrap: missing
`data/` directory and unmigrated database), and TD-015 (concurrent runs corrupt run bookkeeping).

## Validation

* Pytest: **532 / 532 passed** (450 pre-existing unchanged + 82 new)
* Ruff: 13 pre-existing W292/W293 errors (TD-002), no new errors
* Configuration validation: PASS
* Live verification: 250 items collected from all five active sources, run COMPLETED; second run
  collected 0 new items with the count held at 250, confirming exact deduplication against a real
  database
* §4.6 guard verified against a real SQLite file by injecting a RUNNING row and confirming
  reconciliation to FAILED on the next run
* G4 verified end to end against a real database: setting `github_trending.enabled: false`
  produced a run with four sources and zero rows carrying that `source_id`
* Piped CLI output verified: `arip run` exits 0 when stdout is redirected
* SDS compliance reviewed; architecture review completed; test review completed

## Phase 2 Quality Gate

All five gate items are met, as governed by §8.1 and §8.3:

* **G1** — five registered sources fetch and normalize real data (250 items, live)
* **G2** — `SourceRegistry` loads only enabled sources
* **G3** — exact duplicates discarded before insertion; second run added 0
* **G4** — verified end to end: `github_trending` set to `enabled: false`, fresh database, run
  produced four sources and no rows for that source
* **G5** — a source failure yields an empty list and the run continues; a normalization failure
  marks that item FAILED and collection continues

**Phase 2 is complete.**

## Git

Commits:

`8b2a24a` — `docs(sds): add 5.14.1, 8.1-8.3, 9.2 deferred decision; add TD-010-012`
`5fd843d` — `feat(batch7): add RawPayloadRepository and PipelineRunRepository`
`7d16ba9` — `feat(batch7): add CollectStage`
`ee8fabc` — `docs: defer SDS 3.3 normalization retry; add TD-013`
`8c0a80b` — `feat(batch7): implement PipelineOrchestrator.run_once and wire the CLI`

## Next

Batch 8

* RankStage — the next SDS Phase 1 stage per §8.2

Prerequisites:

* Batch 7 committed and pushed
* Working tree clean
* Batch 8 scope defined before implementation — "the next stage" is not a specification, and the
  same Phase 1 analysis that Batch 7 required applies again

---

# Batch 8 – Ranking (Scorer and RankStage)

## Summary

Implemented the ranking half of SDS Phase 3: the four-signal composite `Scorer` and the `RankStage`
that applies `ranking.min_score`. This is the first batch in which items are evaluated rather than
merely collected, and the first in which the frozen ranking weights produce a visible outcome.

Phase 1 analysis found that the SDS did not determine Batch 8's scope. §8 Phase 1 lists
`RankStage (stub: score = 1.0 for all items)`; §8 Phase 3 lists a real `Scorer`. A stub would have
satisfied no quality gate in any phase — Phase 1's needs every stage through to ARCHIVED, Phase 3's
needs real signals — and Phase 1's stated purpose, proving the orchestration skeleton, was already
met by Batch 7 against live data. Scope option B was approved: build the real thing once, and close
Phase 1 by amendment rather than build stubs whose only future is deletion.

## Implemented

* Added `Scorer` (`arip/ranking/scorer.py`) — recency, source authority, engagement and topic
  relevance, weighted per `config.ranking.weights`
* Added `RankStage` (`arip/pipeline/stages/rank.py`) — `COLLECTED → RANKED` / `FILTERED`
* Added `recency_k: float = 0.15` to `RankingSettings` and surfaced it in `config/settings.yaml`
* Wired `Scorer` into `container.py` and `RankStage` into `PipelineOrchestrator.run_once()`
* `CollectStage` now stamps `normalized_at`, a column previously written by no code in the project
* Added 92 unit tests

## Decisions

Three judgement calls the SDS does not settle. None has a home in the SDS or the debt register, so
they are recorded here.

* **`Scorer` returns; `RankStage` decides.** §5.5 lists "Filter items below threshold" among the
  ranking responsibilities, which could have put `min_score` inside the scorer. It is in the stage
  instead. The scorer computes and returns `(score, breakdown)` with no knowledge of the threshold;
  the stage compares and transitions. This is why `tests/unit/test_scorer.py` needs no database at
  all — 63 tests of pure arithmetic — and why the threshold can be changed without touching scoring
  logic.
* **Scoring is batch-scoped, with no single-item public method.** The engagement signal is a
  percentile within the current run (§5.5), which is undefined for one item in isolation. Rather
  than offer a `score(item)` that silently means something different from `score_batch([item])`,
  `score_batch` is the only public entry point. §5.5's `(score, breakdown)` return shape is
  preserved per item.
* **The orchestrator receives collaborators, never a settings object.** `RankStage` needs a
  `Scorer`, which needs the whole `RankingSettings`. Rather than thread `AppSettings` into
  `PipelineOrchestrator` — which would let every future stage reach for whatever config it liked —
  `container.py` builds the `Scorer` and passes it in already constructed, alongside a bare
  `min_score: float`. This is the pattern Batch 7 established for `SourceRegistry`, applied a
  second time rather than a new one invented. The cost is a mixed-granularity constructor: a
  constructed object beside a bare float.

Additional derived decisions, smaller in scope:

* Items whose source exposes no engagement metric (`arxiv`) score 0.0 **and are excluded from
  cohort statistics**. Counting them as zeros would let a metric-less source inflate the
  percentiles of every other source sharing its `source_type`.
* A cohort of one scores engagement 1.0 (§5.5.1); a cohort tied at one value scores 0.0 for all
  members. The asymmetry is deliberate — see TD-017.
* `_filter()` persists `importance_score` only, per §3.5. The breakdown is logged at INFO on
  `item_filtered` so the reason an item fell short stays recoverable from the run log.

## Guidance for the embedding stage

**`signal_breakdown` must be read, merged and re-written — never overwritten.**

`RankStage` is now the first writer of `items.signal_breakdown`, storing the four ranking signals as
JSON. SDS §5.5 states that the ANN distance to the nearest neighbour is *also* stored in that
column ("stored in `items.signal_breakdown` but is not used in the ranking score"). The stage that
computes it must load the existing JSON, add its key, and write the merged object back. A plain
`json.dumps({"novelty": d})` would silently discard the four ranking signals for every item, with
no error and no failing test.

`ItemRepository.get_all_with_embeddings()` also already exists and is currently dead code — it was
written for the §5.7 startup reconciliation query and is waiting for its caller. Verified in Batch
8: the only references to it in the tree are its definition and one unit test.

## SDS References

* §3.3 — `COLLECTED → RANKED` / `FILTERED` actions and guards
* §3.4 / AD-03 — `StateMachine.transition()` as sole status mutator
* §3.5 — `COLLECTED → FILTERED` sets `importance_score` only
* §4.2 — `importance_score` [0.0–1.0]; `signal_breakdown` TEXT (JSON)
* §5.5 — the four signals, weights, and failure modes
* §5.5.1 — engagement metric per source, single-item cohort, topic match text
* §5.14 — repositories; session per unit of work
* §5.15.1 — `recency_k` configuration field
* §5.16 — structured logging with bound context
* §6 — mandated file paths for the scorer and the stage
* §8.2 — batch/phase mapping
* §8.4 — Phase 1 stub stages superseded; Phase 1 closed
* §8.5 — `COLLECTED` fan-out across two stages; `normalized_at` ownership
* §9.1 AD-19 — novelty is not a ranking signal
* §9.2 — deferred: `max_items_per_run` enforcement point, normalization retry

## Files Changed

| File | Reason |
|------|--------|
| `arip/ranking/__init__.py` | NEW — package marker (§6) |
| `arip/ranking/scorer.py` | NEW — `Scorer` (§5.5, §5.5.1) |
| `arip/pipeline/stages/rank.py` | NEW — `RankStage` (§6, §8.5) |
| `arip/config.py` | MODIFIED — `recency_k` field (§5.15.1) |
| `config/settings.yaml` | MODIFIED — `recency_k` surfaced |
| `arip/pipeline/stages/collect.py` | MODIFIED — stamps `normalized_at` (§8.5) |
| `arip/pipeline/orchestrator.py` | MODIFIED — `_rank()` and the constructor's scorer/min_score |
| `arip/container.py` | MODIFIED — builds `Scorer`, wires it into the orchestrator |
| `tests/unit/test_scorer.py` | NEW — 63 scorer tests |
| `tests/unit/pipeline/test_rank.py` | NEW — 21 stage tests |
| `tests/unit/pipeline/test_orchestrator.py` | MODIFIED — constructor sites; 6 ranking tests |
| `tests/unit/pipeline/test_collect.py` | MODIFIED — 2 `normalized_at` tests |

No new dependency. No configuration schema change beyond the amended `recency_k`. No source plugin
modified.

## Technical Debt

No new technical debt introduced.

Recorded separately: TD-016 (ArXiv ages out without an engagement metric), TD-017 (all-equal
cohorts score 0.0), and TD-018 (recency measures creation age, not activity — confirmed on live
data as the driver behind `huggingface_spaces` filtering).

## Validation

* Pytest: **624 / 624 passed** (532 pre-existing unchanged + 92 new)
* Ruff: 13 pre-existing W292/W293 errors (TD-002), no new errors
* Configuration validation: PASS
* Coverage: `scorer.py`, `rank.py` and `orchestrator.py` all at 100%
* Live verification: 250 items collected from five sources, **183 RANKED / 67 FILTERED**, zero left
  in `COLLECTED`; all 67 filtered rows carry a NULL `signal_breakdown`, confirming the §3.5
  narrowing on real data
* Per-source mean scores: `huggingface_papers` 0.608, `github_trending` 0.495, `arxiv` 0.482,
  `huggingface_models` 0.356, `huggingface_spaces` 0.294

## Next

Batch 9

* Embeddings and semantic deduplication — the remaining half of SDS Phase 3

Prerequisites:

* Batch 8 committed and pushed
* Working tree clean
* Batch 9 scope defined before implementation, per the §8.2 precedent
* Two new dependencies to be reviewed before adoption: `sentence-transformers`, `usearch`
* The `signal_breakdown` merge requirement above must be honoured by `EmbedStage`
