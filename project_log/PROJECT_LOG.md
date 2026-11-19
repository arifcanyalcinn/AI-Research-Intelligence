# Project Log

## Current Status

Current Phase: Phase 1
Current Batch: Batch 9 (Completed)
Next Batch: Batch 10 (content generation with a local LLM)
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

- Batch 10
- Content generation with a local LLM (SDS Phase 4)

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

---

# Batch 9 – Embeddings and Semantic Deduplication

## Summary

Phase 3 complete. Embedding backends, the ANN deduplicator, and the EmbedStage
that drives them. The Phase 3 quality gate is fully closed: Batch 8 closed gate
items 1, 2, 5 and 6; this batch closes 3 and 4 with production evidence.

Delivered in four stages: dependencies, embedding backends, SemanticDeduplicator,
EmbedStage and wiring.

## Implemented

- arip/backends/__init__.py, arip/backends/embeddings/{__init__,registry,
  stub_backend,sentence_transformers_backend}.py
- arip/dedup/{__init__,semantic}.py
- arip/pipeline/stages/embed.py
- pyproject.toml: [project.optional-dependencies] embedding
- Modified: arip/interfaces.py (two additive Phase 0 changes),
  arip/pipeline/orchestrator.py, arip/container.py
- Tests: tests/unit/backends/test_embeddings.py (41),
  tests/unit/dedup/test_semantic.py (37),
  tests/unit/pipeline/test_embed.py (30),
  tests/unit/pipeline/test_orchestrator.py (+7)

## Decisions

Recorded in full in project_log/batches/BATCH-09-DECISIONS.md. In brief:

- A1  embed() returns np.ndarray. The Phase 0 ABC changed because nothing
      implemented BaseEmbedder yet — the only moment at which that was free.
- A2  Check first, add after. SDS 5.7 governs over 3.3. The other reading makes
      every item its own duplicate at similarity 1.0 and destroys the run.
- A7  usearch is NOT pure Python (measured: compiled extension modules). The
      SDS's stated rationale is wrong; its conclusion stands. Not amended.
- A9  Novelty is merged into signal_breakdown, never overwritten, and is not a
      ranking signal (AD-19).
- Write order: the database commits before save_index(). An index entry for a
      row the database never recorded is the one divergence reconciliation
      cannot repair.
- Dependencies: sentence-transformers 6.0.1 and usearch 2.26.2, chosen together.
      The recorded 3.0.1 pin was kept only for transformers 4.43 compatibility,
      and that was measured false — 3.0.1 resolves against transformers 4.57.

## Defects found and fixed within the batch

- Stage 2: StubEmbedder recorded itself as the real model. The registry passes
  config.model_name to every backend, so the stub's default never applied and a
  stub run wrote embedding_model_name = "all-MiniLM-L6-v2". SDS 5.7's
  reconciliation selects on embedding_computed_at alone and cannot detect that.
  Fixed by prefixing: model_name returns "stub:<model>". The test that should
  have caught it asserted on a directly-constructed StubEmbedder — the one path
  production never takes.
- Stage 3: the threshold-boundary test passed under both >= and >, because
  float32 puts the constructed 0.92 vector at 0.9200000017881393. Replaced with
  a threshold of 1.0 against an identical vector, the only exact equality
  available. Found by mutation, not by reading.
- Stage 4: the write-order test passed with save_index() moved inside the
  session scope, because in-memory SQLite shares one connection across sessions.
  Rebuilt on a file-backed database.

## SDS References

5.6, 5.7, 3.3 (RANKED->EMBEDDED, RANKED->FAILED, EMBEDDED->ENRICHED,
EMBEDDED->DUPLICATE), 5.5 (novelty), 5.14, 5.15, 5.16, 1.2, 11, AD-02, AD-03,
AD-06, AD-11, AD-19, D-004, D-005.

## Technical Debt

TD-019 (Stage 1, stale llm pin line), TD-020 (HF Models semantic false
positives), TD-021 (usearch accepts NaN vectors silently). TD-018 confirmed in
production data.

## Validation

739 passed with the embedding extra; 661 passed, 2 skipped without it — the two
skips each stand for a whole module.
13 ruff errors, all pre-existing W292/W293 (TD-002), unchanged.
100% statement coverage on every module this batch added.
Eight mutations run; each failed only the tests that claim to protect it.

## Live run

399 items: 306 ENRICHED, 76 FILTERED, 17 DUPLICATE, zero left in RANKED.
323 embedded = 306 + 17; the index holds 306 — duplicates are never indexed.
ANN index 515,484 bytes, 306 vectors, written after the commit.
13 of 17 duplicates cross-source, every one huggingface_papers matched to arxiv.
200 of 200 sampled rows carry all five signal_breakdown keys — the read-merge-
write defect warned about since Batch 8 is absent in production.
Four false positives, all huggingface_models — see TD-020.

## Phase 3 Quality Gate

CLOSED. Six actionable items met; the "NOT YET" constraint holds.

## Next

Batch 10

* Content generation with a local LLM (SDS Phase 4)

Prerequisites:

* Batch 9 committed and pushed
* Working tree clean
* TD-020 decided before generation begins — Phase 4 feeds this pool to the LLM
* TD-019: the commented llm pin line re-pinned before it is uncommented


---

# Batch 10 – Ranking Calibration

## Scope decision (2026-10-25)

Option (b), reduced scope, chosen over the full calibration.

In scope:

* Cheap cleanup: TD-002, TD-006 (decide), TD-008, TD-014
* TD-016: what a source with no engagement metric receives (SDS §5.5.1 amendment)
* TD-017: assessed in the same amendment if the analysis shows it belongs there
* Re-scoring path for the existing pool
* GitHub AI-topic filter: analysis question only; included if it needs no SDS
  amendment and fits in about half a working day

Deferred to after Phase 4: TD-003, TD-004, TD-005, TD-018.

Rationale, measured on the 306-item ENRICHED pool:

* Re-scored with arxiv engagement = 0.5, the top 50 changes from
  35 hf_papers + 15 github (10 with zero topic match) + 0 arxiv
  to 21 hf_papers + 26 arxiv + 3 github (1 with zero topic match).
* huggingface_models and huggingface_spaces rank 104th at best and reach the
  top 50 in no scenario, so TD-003/004/005/018 do not change the near-term digest.
* Measured pace, Batches 7-9: about one working day per stage.
  Option (b) is 4-5 working days, option (a) 7-9.

Findings recorded during scoping:

* `github_trending._build_query()` returns only `created:>DATE`; there is no
  AI-topic filter. 70 of 83 github items in the pool have a topic score of 0.
* The module docstring of `arip/sources/github_trending.py` quotes an
  "SDS §5.3.1" rule on acquisition semantics that does not appear in
  `docs/frozen_sds.md/frozen_sds.md`. §5.3.1 in the frozen SDS is the
  secret-management exception.

Next: Batch 10 PRE-CODE ANALYSIS, then Batch 11 = SDS Phase 4.

## Rulings (2026-10-25, on the PRE-CODE analysis)

- A1/A3: TD-016 option (c), fixed engagement value 0.35 for declared metric-less
  sources, via a new SDS amendment §5.5.2 (text approved verbatim before it is applied).
  Floor 0.335 < min_score 0.35: age-out survives (~day 19 without topic match).
  Simulated top 50 (ENRICHED, 306): 30 HF papers + 13 arxiv + 7 GitHub.
- A2: explicit named set, module constant; only arxiv. Unknown sources stay 0.0.
- A4: TD-017 no behaviour change; amendment records it as considered and not changed.
- A6: re-score via a fresh DB and one live run. arip.db, -wal, -shm and
  ann_index.usearch archived together. Before/after computed read-only from the archive.
- A7: GitHub AI filter deferred (TD-022). Live check 2026-10-25: OR between topic:
  qualifiers -> HTTP 422; OR between bare keywords works (llm 13,699 + diffusion 475
  -> 14,146). Wrong "§5.3.1" citations corrected in Stage 1, comments only.
- A8: ruff format adopted as standalone Stage 1b, before any logic stage.
- A8b: "Decision 001-005" kept as the historical register; new decisions continue at D-007.
- A9: TD-014 documentation only (mkdir data logs before alembic upgrade head).

## Ruling change (2026-10-25, Stage 3 PRE-CODE)

- R1: the TD-016 declared value is 0.24, superseding 0.35 above. At 0.35 an
  arxiv item with one topic match has floor 0.385 and never ages out (59% of
  the pool). At 0.24 the floors are 0.2965 / 0.3465 / 0.3965 for 0 / 1 / 2
  matches; items with at most one match (67%) still age out (day 11 / day 29).
  Same simulated top 50 as 0.35: 13 arxiv + 30 HF papers + 7 GitHub.
- Stage 2 "before" measurement (production snapshot, ENRICHED 306) matches the
  reviewer's figures. arxiv FILTERED = 0, so in this pool the change affects
  ordering only. Ties at the top-50 cutoff (same-day arxiv items score
  identically). GitHub items with topics: 29 of 83 (34.9%).

## Delivery

Five stages and one decision commit, 5026ead → 22a4b9d.

| Stage | Content | Commit |
|---|---|---|
| 1 | Cleanup: TD-002 (`ruff check --fix`, whitespace only), TD-008 (`.env.example` Search API limits), TD-014 (README Setup section), and the non-existent "SDS §5.3.1" quotations in `github_trending.py` and its tests replaced with real citations (docstrings and comments only; syntax trees otherwise identical) | ac2de6d |
| 1b | `ruff format` adopted: 27 files; 7 adjacent-literal pairs merged by hand first; syntax-tree proof with docstrings normalised (one file differs in docstring whitespace only) | 93ed3cf |
| — | D-007: `ruff format --check` added to the gate | 99100cc |
| 2 | `tools/measure_ranking_pool.py` (new): read-only snapshot + measurement | 92c0d2f |
| 3 | Commit 1: split `test_metricless_source_is_excluded_from_cohort_statistics`, no behaviour change. Commit 2: SDS §5.5.2 (85 lines, additions only; block SHA-256 e9f5db39…), `METRICLESS_SOURCES = {"arxiv"}`, `METRICLESS_ENGAGEMENT = 0.24`, changed and new tests | 01c5ef3, 3ddc6fd |
| 4 | Tool reads the declared value from `arip.ranking.scorer`; declared-value check; corrected FILTERED projection; stored-baseline comparison; fresh DB and one live run | 22a4b9d |

Rulings for every stage are recorded in `project_log/batches/BATCH-10-DECISIONS.md`.

## SDS

One amendment: §5.5.2 "Engagement for a Source Without an Engagement Metric", inserted after
§5.5.1. It amends the §5.5 Engagement row and the §5.5.1 "absent, NULL, or missing its key"
sentence; both original texts are left in place. No §9.1 decision is altered. No other SDS text
changed.

## Validation

* pytest 739 → 740 (Stage 3 commit 1) → **750** (Stage 3 commit 2)
* `ruff check`: 13 → **0**; `ruff format --check`: 28 files → **0** (69 formatted)
* `check-config` valid at every stage
* Stage 3:
  * The 75-test must-not-change set passes on both the old and the new scorer.
  * The 15 changed or new tests fail individually on the old scorer.
  * Mutations M1–M9 each failed exactly the predicted tests. M4 (0.24 → 0.25) also flips the
    one-match floor across min_score, which confirms the boundary.
  * `scorer.py` coverage 100%.
* Tool check script (outside the repository): 36 checks, including breakages B1–B7

## Live run and measurement

Fresh database (archive in `%USERPROFILE%\arip-archive\batch10-before-2026-10-28`: arip.db,
an empty arip.db-wal, arip.db-shm, ann_index.usearch; hashes matched; an archive snapshot
reproduced the Stage 2 snapshot SHA-256 exactly). Pre-flight checks clean three times. One run:
"✓ Pipeline run 1 complete.", exit 0. The embedding model was not cached and was re-downloaded
during the run. Criteria S1–S9 all pass.

| | Before: archived pool, previous rule | After: fresh DB, one run, §5.5.2 |
|---|---|---|
| Snapshot SHA-256 | 0024eb7f… | 7a7d6541…5103 |
| Runs that built it | ≥ 2 (arxiv 100 = 2 × 50) | 1 |
| Rows | 399 | 250 (50 per source) |
| ENRICHED | 306: arxiv 100, github 83, hf_models 24, hf_papers 83, hf_spaces 16 | 179: arxiv 50, github 46, hf_models 24, hf_papers 43, hf_spaces 16 |
| FILTERED | 76: spaces 34, models 25, github 13, hf_papers 4 | 63: spaces 34, models 23, github 4, hf_papers 2 |
| DUPLICATE | 17 | 8: hf_papers 5, hf_models 3 |
| arxiv FILTERED | 0 | 0 |
| arxiv stored engagement | 0.0 | 0.24 × 50 (rule detected: §5.5.2) |
| Integrity (stored score = breakdown × weights) | OK | OK, max diff 0.0 |
| Stored top 50, arxiv / github / hf_papers | 0 / 15 / 35 | 15 / 12 / 23 |
| Same items under the other rule | 13 / 7 / 30 (at 0.24) | 5 / 17 / 28 (at 0.00) |
| Ties at the top-50 cutoff | ranks 50/51 tie at 0.6509 (v = 0.35); 20 rows tie (v = 0.40, 0.50) | ranks 50/51 tie at 0.5906 |
| GitHub ENRICHED items with topics | 29 / 83 (34.9%); top: ai-agents 6 | 20 / 46 (43.5%); top: claude-code 7, codex 5, agent-skills 4, cursor 3 |

Comparisons of the same items are made only within one database, under two rules. Across the
two databases only statistics are compared: different items, a different week, one run against
at least two.

## Findings

* **TD-016 changes ordering, not admission.** No arxiv item was FILTERED in either pool. On the
  fresh pool the declared value puts 15 arxiv items in the top 50, against 5 under the previous
  rule on the same items.
* **Ties at the top-50 cutoff** occur in both pools; same-day arxiv items with identical
  breakdowns score identically. → TD-023.
* **`pipeline.max_items_per_run` is enforced nowhere.** One run produced 179 ENRICHED items. → TD-023.
* **GitHub composition.** The 7-day window is dominated by coding-agent tooling, plus items
  tagged "uncensored-ai"; 65.2% of GitHub items have topic score 0. Whether these are in scope
  for an AI-research digest is a product decision, not taken here. → TD-022.
* **TD-020 likely recurs.** 3 hf_models DUPLICATE rows appeared in the fresh run. The pairs were
  not inspected.
* **Snapshot hazard, confirmed by test.** A database-only copy taken while the WAL holds
  committed data silently omits that data. The tool's snapshot subcommand avoids it, and the
  fallback is documented in the tool.
* **Windows App Control** blocks pip-generated launcher .exe files. pytest and alembic are run as
  `python -m pytest` and `python -m alembic`; ruff.exe is unaffected. The README Setup section
  still says `alembic upgrade head`, which will likely fail on the target machine (alembic.exe
  is the same kind of pip launcher as the blocked pytest.exe; not tested).
* **Cosmetic:** under Windows PowerShell, `Tee-Object` shows "§" as "┬ğ". The tool writes UTF-8;
  PowerShell 5.1 decodes native output with the console code page. Setting
  `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` first is the likely fix (NOT
  VERIFIED on the target machine). No debt recorded.
* **Reported in the pre-code analysis, not acted on.** The `alembic/env.py` docstring says the
  fallback is in-memory, but the code falls back to `data/arip.db`. `_ensure_runtime_dirs`
  hard-codes `data/`. README.md status is stale. Not registered as debt; the reviewer decides.

## Technical Debt

Removed: TD-002, TD-006, TD-008, TD-014, TD-016. Updated: TD-017 (the §5.5.2 reference) and
TD-022 (topic measurements). New: TD-023 (top-N tie-break; `max_items_per_run` enforced nowhere).

## Next

Batch 11 — SDS Phase 4, local LLM generation. Prerequisites:

* Batch 10 closure blocks applied; working tree clean.
* Phase 4 pre-code analysis, with scope defined per the §8.2 precedent.
* **TD-019**, Stage 1 alone: re-pin the commented `llm` extra as a whole, including a CUDA build
  of torch for Windows. Dependency change only, no code.
* **LLM model.** `config/settings.yaml` names `Qwen/Qwen2.5-7B-Instruct`; the 8 GB-VRAM plan is
  `Qwen2.5-3B-Instruct` (4-bit), per §7.1's 8 GB guidance. `model_name` is a config field, so no
  amendment is needed for the value. Decide the model, and whether §7.1 itself needs amending.
* **TD-020** decided before generation consumes the pool.
* **TD-023** decided before Phase 4 selects items: the cap's enforcement point (closing §9.2's
  deferral) and a deterministic tie-break.
* **TD-022**, recommended: decide whether GitHub items outside AI research (coding-agent tooling,
  "uncensored-ai") should reach generation.
* **Windows App Control:** every command written as `python -m …`. Correct the README Setup line
  to `python -m alembic upgrade head` in Stage 1.
* The LLM weights (several GB) are downloaded on first use; that run needs network access and
  disk space.

  
# Batch 11 – Local LLM Generation, SDS Phase 4 (in progress)

## Decisions before PRE-CODE (2026-10-29)

- LLM model: Qwen2.5-3B-Instruct, 4-bit (SDS §7.1, 8 GB guidance). Closed; not to be
  re-discussed. config/settings.yaml currently names Qwen2.5-7B-Instruct; changed in a
  Batch 11 stage. To be recorded as D-008.
- TD-020 (HF Models semantic false positives) is resolved in Batch 11.
- TD-023 tie-break, in principle: importance_score desc, then engagement desc, then
  topic desc, then published_date desc, then id asc. Exact ties exist between same-day
  arxiv items with identical breakdowns, so the last two keys are required.
- Vize demo (after ~2026-11-15) presents the Batch 10 state, tagged `batch10`.
  Every Batch 11 stage must keep `python -m arip run`, `tools/review_pool.py` and
  `tools/measure_ranking_pool.py` working.

## Rulings (2026-10-29, on the PRE-CODE analysis)

The analysis was accepted. Direction-setting rulings only:

* **A1** – direction (ii): a config switch, default off, so `python -m arip run` behaves exactly
  as today unless enabled. Name, location and the §5.15 amendment are ruled in Stage 8's PRE-CODE.
* **A22** – a separate venv for Batch 11, outside the repository, at
  `C:\Users\ASUS\venvs\arip-llm`. The demo venv `.venv` is never modified.
* **TD-020** – direction (b): exclude `huggingface_models` and `huggingface_spaces` from the
  semantic duplicate decision and from the index. Final after the Stage 2 measurement.
* **TD-023 (b)** – direction: enforce `pipeline.max_items_per_run` at generation selection. Final,
  with amendment text, after Stage 2.
* **A21** – project_log files: the user sends the current files; the implementer edits them and
  returns them in a zip with a manifest and a list of every removed line.
* **A23** – the model licence (`qwen-research`) is recorded in D-008's scope; ruled before Phase 6.
* **D-008** – draft approved as written; delivered for DECISIONS.md in Stage 5 together with the
  `settings.yaml` change.
* **Stage split 1–9** – approved as proposed.
* **A2–A20** – deferred to the PRE-CODE of the stage that needs each. Input only, not rulings:
  `batch11-ambiguities-dossier.md` (outside the repository, SHA-256 433cee64…e5d8, 2026-11-14).

## Stage 1 – Dependencies, TD-019 (2026-10-29 to 2026-11-16)

Commit 12bf3e5. Dependency and documentation change only; no code, no test change.

* `pyproject.toml`: the stale commented `llm` line replaced by a live extra, `torch==2.14.1`,
  `transformers==5.20.2`, `bitsandbytes==0.50.2`, `accelerate==1.15.0`, with the Windows install
  order in a comment. The leftover heading for the later extras now reads "Phase 5+".
* `README.md`: Setup line is `python -m alembic upgrade head`.
* `CURRENT_STATE.md`: the stale "306 ENRICHED" thermal note corrected (179; cap direction).
* Outside the repository: `check_llm_env.py` (SHA-256 fb70f842…00c7) and a PowerShell command
  sheet.

### Install (new venv `C:\Users\ASUS\venvs\arip-llm`, Python 3.11.9)

The demo venv `.venv` was not modified (ruling A22). torch 2.14.1+cu126, CUDA 12.6,
`torch.cuda.is_available()` True. The CUDA wheel was 2.6 GB from the PyTorch index at about
2.4 MB/s; PyPI downloads ran at only 20–60 kB/s. `pip check` clean. Gate in the new venv:
`ruff check` 0, `ruff format --check` 69 files formatted, `check-config` valid, pytest **750**.
`Qwen/Qwen2.5-3B-Instruct` downloaded to the Hugging Face cache.

### Environment finding: Windows Smart App Control

Smart App Control was on and blocked unsigned native libraries. usearch's compiled `.pyd`
(identical SHA-256 in both venvs) failed to load in **both** venvs, including the demo `.venv`,
where it had still loaded on 2026-11-14; `libbitsandbytes_cuda126.dll` failed in the new venv
(WinError 4551). The pip launcher .exe blocks recorded in Batch 10 as "Windows App Control" came
from the same feature. Why usearch stopped loading between 2026-11-14 and 2026-11-16 was not
determined. The user turned Smart App Control off on 2026-11-16; afterwards
`python -m pytest -q --no-cov` gives 750 passed in both venvs. The `python -m …` rule stays.
Tested fact for the demo: while Smart App Control was on, usearch's compiled module failed to
import in both venvs, and `libbitsandbytes_cuda126.dll` failed to load.

### Check results (as reported by the user, 2026-11-16)

Part 1 (no model): bitsandbytes CUDA backend, `libbitsandbytes_cuda126.dll`, NF4 packing, quant
type and forward-pass checks all pass.

Part 2 (`Qwen/Qwen2.5-3B-Instruct`, 4-bit NF4, Hub offline):

| Measurement (compute dtype bfloat16) | Value |
|---|---|
| Load time | 6.2 s |
| Linear4bit layers, quant type | 252, nf4 |
| `get_memory_footprint()` | 1917 MiB |
| VRAM allocated after load | 2095.9 MiB |
| Greedy generation | 30.8 tok/s, stop = eos, sensible text |
| `max_new_tokens=8` | stops as max_tokens |
| Same seed / different seed | reproducible / differs |
| Peak VRAM allocated by torch | 2134.8 MiB of 8188 MiB |
| nvidia-smi before / after load / after unload | 732 / 2989 / 895 MiB |

The same run with compute dtype float16 gave the same results at 29.4 tok/s.

One check failed in every run: "allocated 8.1 MiB, reserved 22.0 MiB after empty_cache" (part 1,
a 256×256 test layer) and "allocated 8.1 MiB, reserved 44.0 MiB" (part 2, the full model). The
check's limit is 16 MiB on each figure, so it failed on *reserved*, not on the 8.1 MiB. The
8.1 MiB is the same for a tiny layer and a 2 GB model, so it is not model memory. It is the
cuBLAS workspace that PyTorch keeps per process after the first matrix product
(2 × 4096 KiB + 8 × 16 KiB = 8.125 MiB); the reserved figure is the allocator segments that hold
it. Confirmed by a diagnostic on 2026-11-16 in the `arip-llm` venv: after one 64×64 matrix
product and `empty_cache()`, allocated / reserved = 8.125 / 20.0 MiB; after
`torch._C._cuda_clearCublasWorkspaces()` and `empty_cache()`, 0.0 / 0.0 MiB. The check script's
limit on reserved memory was too strict; the script was not changed. Consequence for
ruling A20 and Stage 5: a "VRAM released" assertion compares allocated memory against the value
before the model was loaded, with a small tolerance, and does not require reserved memory to be
zero.

Data for ruling A15 (Stage 5 PRE-CODE): bfloat16 30.8 tok/s against float16 29.4 tok/s; peak
2134.8 MiB of 8188 MiB. Double quantization was not measured.

TD-019 → Removed Debt. An ambiguity dossier for A2–A20 was prepared outside the repository
(`batch11-ambiguities-dossier.md`, SHA-256 433cee64…e5d8); its recommendations are input for the
stage PRE-CODEs, not rulings. Correction to it: the rule cited for A12 is in SDS §5.4, not §5.2.

## Stage 2 – Read-only measurements for TD-020 and TD-023

### Rulings (2026-11-16, on the Stage 2 PRE-CODE)

* **Q1** – one tool: `tools/measure_generation_pool.py`, with subcommands `dedup`, `selection`
  and `compare`.
* **Q2** – approved: a read-only snapshot of `data/arip.db` through the existing
  `tools/measure_ranking_pool.py snapshot` (SQLite backup API), as in Batch 10.
* **Q3** – the read-only helpers are copied into the new tool; `measure_ranking_pool.py` is not
  modified (the demo depends on it).
* **Q4** – NULL `published_date` in the tie-break: show both placements (first and last) and
  report whether it matters.
* **Q5** – the one-line embedding-text formula is repeated in the tool; the check script asserts
  that it equals the embed stage's output on fixtures.
* **Q6** – the TD-020 measurement runs in both venvs (`.venv`, then `arip-llm`), followed by
  `compare`.

### Delivered

* `tools/measure_generation_pool.py`, read-only, with `selection`, `dedup` and `compare`. First
  version SHA-256 d27cbeb2…c8dd; fixed version (stage 2b, below) SHA-256 db22a2d6…ec32.
  `tools/measure_ranking_pool.py` and `tools/review_pool.py` unchanged.
* Outside the repository: `check_measure_generation_pool.py` (stage 2b version SHA-256
  53a9f18f…f483) and a PowerShell command sheet. No application code and no test changed; pytest
  stays at 750.

### Windows finding: tied timestamps, and the fix (stage 2b)

On Windows the first version of the check script failed its dedup hand-value comparison and the
control check. Cause: the replay was ordered by `embedding_computed_at`, and on Windows those
values tied for items processed in quick succession (the made-up pool embeds ten items in a few
milliseconds). Among equal values the replay fell back to id order, which is not the order the
stage processed them in. Reproduced on Linux by setting every timestamp equal: the old tool then
mismatched 8 of 10 rows. The real pool was not affected: each real embedding takes longer than a
clock tick, so its timestamps were distinct and the first run reproduced 187/187.

Fix: the replay now uses the embedding stage's own query order, `collected_at` then `id`
(`EmbedStage.run` processes `ItemRepository.get_by_status(RANKED)`, which orders by
`collected_at` only). `embedding_computed_at` is used only to check that order; any pair embedded
in the opposite order is reported as a contradiction. The check script repeats its dedup hand
values on a copy with every timestamp equal and on one with a reversed pair, and two new
deliberate breakages are caught (B14 timestamp ordering, B15 contradiction check disabled).

Finding about the stage: SQL leaves the order among RANKED items with equal `collected_at`
undefined. That order decides which item of a near-duplicate pair survives. SQLite returned ties
in id order in a test (index on status, then a sort), which is not a guarantee. Fix ruled in R2.

### Results (2026-11-17)

* Check script on Windows (demo venv, Python 3.11.9): 59/59 PASS.
* Snapshot SHA-256 7a7d6541…5103, identical to the Batch 10 Stage 4 state, unchanged by every run.
* `selection`: 179 ENRICHED (arxiv 50, github_trending 46, huggingface_models 24,
  huggingface_papers 43, huggingface_spaces 16). No NULL in any field generation needs, so NULL
  date placement changes nothing. 60 rows in 9 `importance_score` tie groups; engagement
  separates 10; topic and published_date separate none; id decides the remaining 50 rows (4 groups
  of exact ties). Cutoff at 50: a 2-row tie, separated by engagement. Cutoff at 100: a 16-row tie,
  separated only by id. Pass 1 is arxiv 15, github_trending 12, huggingface_papers 23, identical
  to the id-only order and to the Batch 10 stored top 50. Draining the pool with no new items
  takes 4 passes; passes 2–4 hold items published as far back as 2022-03-02.
* `dedup`, both venvs: 187 embedded rows; replay reproduces 187/187; `embedding_computed_at` ties
  0; `collected_at` ties 98 rows in 29 groups; no order contradictions.
  * The 3 stored `huggingface_models` duplicates are size or version variants: Qwen/Qwen3-8B →
    Qwen/Qwen3-0.6B 0.9282; Qwen/Qwen3.5-4B → Qwen/Qwen3-0.6B 0.9463;
    google/gemma-4-26B-A4B-it → google/gemma-4-31B-it 0.9693. All 6 model–model pairs ≥ 0.92 are
    variants.
  * The 5 stored `huggingface_papers` → `arxiv` duplicates are all at 1.0000; the nearest
    non-duplicate between those two sources is 0.8030.
  * 0 cross-source pairs ≥ 0.92 involve `huggingface_models` or `huggingface_spaces`; their
    highest similarity to any other source is 0.5243 (github_trending × huggingface_models). No
    pair involving a Space reaches 0.85.
  * With both sources excluded: 8 stored duplicates → 5; the 3 model duplicates are the only
    changes.
* `compare`: 17,391 pairs; largest difference 0.000000; 0 pairs on different sides of 0.92. The
  two venvs embed identically.

### Rulings (2026-11-17, on the Stage 2 results)

* **R1** – TD-020: direction (b) is final. Exclude `huggingface_models` and
  `huggingface_spaces` from the semantic duplicate decision and from the ANN index. Basis: the 3
  stored model duplicates are variants; 0 cross-source pairs ≥ 0.92 involve an excluded source;
  real duplicates are at 1.0000 with the nearest non-duplicate at 0.803. Delivered in Stage 3 with
  SDS amendment §5.7.1, whose text the user approves verbatim before it is applied.
* **R2** – embed-stage order: a deterministic secondary order (id ascending) among RANKED items
  with equal `collected_at`, in Stage 3. Where it sits, and whether it needs a D-record or an
  amendment, is proposed in the Stage 3 PRE-CODE.
* **R3** – TD-023 (b): the cap at generation selection is final; the amendment closing §9.2's
  deferral is delivered in Stage 4. The tie-break order stays as ruled.
* **R4** – the backlog consequence of the cap is recorded as TD-024 (Phase 7); no action.

## Stage 3 – TD-020 and the embedding order (2026-11-17)

### Rulings (2026-11-17, on the Stage 3 PRE-CODE)

* **P1** (a) – an excluded item's novelty is computed with `nearest()`, as for any item.
* **P2** (i) – reconciliation removes excluded vectors from an existing index;
  `SemanticDeduplicator.remove(item_id) -> bool` added.
* **P3** – the secondary order goes into `ItemRepository.get_by_status()`: `collected_at`, then
  `id`. Recorded as D-009.
* **P4** – D-008 (as approved on 2026-10-29) and D-009 delivered together at this closure, in that
  order; the `settings.yaml` model change stays in Stage 5.
* **P5** – the 2026-10-29 decision stands: the vize demo on 2026-11-27 shows the Batch 10 state
  exactly. Stage 3 is committed on `main` as normal; every rehearsal and the demo run from the tag
  (`git switch --detach batch10`, then `git switch main`). No live demo-check run in this stage;
  its verification is the test suite and the mutations.
* **P6** – the 3 DUPLICATE rows in `data/arip.db` are not changed.
* SDS §5.7.1 text approved verbatim, with points 4 and 5 as above.

### Delivered

* `arip/dedup/semantic.py` – `SEMANTIC_DEDUP_EXCLUDED_SOURCES` (`huggingface_models`,
  `huggingface_spaces`, keyed by `source_id`) and `remove()`.
* `arip/pipeline/stages/embed.py` – an excluded item is embedded and gets its novelty from
  `nearest()`, then goes `EMBEDDED → ENRICHED` without `is_duplicate()` or `add()`; it logs
  `semantic_dedup_skipped_excluded_source` (INFO, item id and source id), and
  `embed_stage_completed` carries `dedup_skipped_excluded_source`. Reconciliation leaves
  excluded items out and removes any an older index still holds (`removed_excluded_source`).
* `arip/db/repositories/items.py` – `get_by_status()` orders by `collected_at, id` (D-009).
* `docs/frozen_sds.md/frozen_sds.md` – §5.7.1 inserted after §5.7's Testing Strategy;
  `git diff --numstat` 29 added, 0 deleted.
* Tests: `tests/unit/pipeline/test_embed_excluded_sources.py` (T1–T8, T9 with doubles, T10),
  `tests/unit/dedup/test_semantic_excluded.py` (T9 against a real usearch index file through save
  and reload, T11–T13), and T14 in `tests/unit/test_repositories.py`. T14 also checks the
  generated SQL, because SQLite returns tied rows in id order even without the key.

### Verification

* Outside the repository, `stage3_verify.py` (SHA-256 fd08c87c…82fc): 18/18 PASS.
  `test_embed.py` (30 tests) and `test_semantic.py` (33 tests) byte-identical; in
  `test_repositories.py` all 21 existing functions identical by syntax tree, change an insertion
  only. Full suite 765 passed; 683 passed and 3 skipped without the `embedding` extra.
* Mutations M1–M11, full suite each; the failing set equalled the predicted set every time:
  guard removed; excluded items indexed; guard keyed on `source_type`; `huggingface_papers` added
  to the set; `huggingface_spaces` dropped; reconciliation re-adds; reconciliation does not
  remove; `remove()` a no-op; excluded novelty fixed at 1.0; `id` dropped from the order; `id`
  descending.
* On the user's machine: all 7 file hashes matched; `git diff --numstat` as stated
  (`frozen_sds.md` 29/0); 765 passed; `ruff check` clean; 71 files formatted; `check-config`
  valid. The reviewer compared the inserted §5.7.1 text line by line with the approved text:
  identical, plus the separating blank line. Committed and pushed.
* Line-ending finding: the user's working-tree `arip/dedup/semantic.py` was LF
  (`git ls-files --eol`: `i/lf w/lf`), content identical to HEAD, so its pre-change hash
  differed from the one delivered; the zip replaced it with the CRLF version.

TD-020 → Removed Debt. D-008 and D-009 added to DECISIONS.md. TD-025 recorded: the
human-relevance evaluation is not yet done and is needed before calibration decisions and before
Phase 6. The project log and `batches/BATCH-10-DECISIONS.md` were searched for a planned human
evaluation (a 26-item scoring, a blind expert ranking, a `yayinlanmali_mi` column): none is
recorded as planned, deferred or dropped. `tools/review_pool.py` writes no such column.

Note: `tools/review_pool.py` prints that it wrote `enriched_pool.csv`, but it writes only
`enriched_pool.html`; no code change before the vize.

## Stage 3b – Human-relevance evaluation (2026-11-17 to 2026-11-18)

### Rulings (2026-11-17, on the evaluation PRE-CODE)

* **E1** – a local HTML form that saves `ratings.csv`.
* **E2** – ENRICHED ranks 51–179 split into two equal halves (middle, bottom).
* **E3** – FILTERED breakdowns recomputed with the application's Scorer, accepted only if every
  stored score is reproduced; otherwise an ENRICHED-only comparison, stated in the report.
* **E4** – questions, buttons and progress text in Turkish, in the ruled wording; item texts
  unchanged.
* **E5** – the Stage 2 snapshot (7a7d6541…5103) reused.
* **E6** – Stage 3b, tools only, before Stage 4.

### Delivered

* `tools/evaluate_relevance.py` (SHA-256 b69cf04f…9f4d), read-only, with `sheet` and `analyze`.
  The form shows only row number, type, title and text (URLs replaced by "[link]"); K1–K4
  appear only after G; a content-security policy blocks network access. The key holds row →
  item id, seed, snapshot hash and sheet id.
* Outside the repository: `check_evaluate_relevance.py` (63/63 PASS, B1–B13 caught),
  `check_evaluate_form.py` (headless Chromium, PASS, B14–B16 caught) and a command sheet.
  On the user's machine: check script PASS; 765 passed, ruff clean, 71 files formatted.

### Evaluation (2026-11-17)

One rater scored all 40 items blind (seed 20261117; strata 50 / 64 / 65 / 63). The analysis is
kept byte-for-byte as `project_log/evaluations/2026-11-17-relevance-b11.txt`. The snapshot was
unchanged, and the recomputation reproduced all 250 stored scores, so K1–K4 were compared on all
40 items.

* G against `importance_score`: rho +0.752 [+0.59 … +0.85] (all 40); +0.677 [+0.45 … +0.83]
  (ENRICHED 30).
* Mean G by stratum: top 4.60, middle 3.40, bottom 1.70, filtered 1.10. Top 50: 9 of 10 with
  G ≥ 4 (Wilson 60%–98%). FILTERED: 0 of 10 with G ≥ 4.
* Mean G by source: huggingface_papers 4.86 (n 7), arxiv 4.50 (n 8), github_trending 2.22
  (n 9), huggingface_spaces 1.18 (n 11), huggingface_models 1.00 (n 5).
* Criteria against signals (n 40): engagement +0.064 with G, +0.068 with K2; authority +0.816
  with G; recency +0.680 with G; topic +0.563 with G, +0.588 with K1.
* Text: 16 items were shown without any text, all rated K3 = 1; K3 against text length +0.823.

### Reading (reviewer, 2026-11-18; indications, one rater, n = 40)

* The evaluation measures agreement with one informed rater's judgement on the shown text, not
  ground truth. The rater's K2 (novelty) reflects his own knowledge of the field.
* Overall agreement is strong: rho(G, score) +0.75 [+0.59 … +0.85]; mean G by stratum 4.6 / 3.4 /
  1.7 / 1.1; top 50: 9/10 with G ≥ 4; FILTERED: 0/10 with G ≥ 4 (no interesting item dropped).
* The agreement is driven mostly by source type: mean G hf_papers 4.86, arxiv 4.50, github 2.22,
  hf_models 1.00, hf_spaces 1.18 (the authority column's +0.82 is a source proxy; recency is
  confounded with source through TD-018).
* Engagement (weight 0.35, the largest) shows almost no relation to human interest:
  G–engagement +0.06, K2–engagement +0.07. Recorded as TD-026.
* Text availability: 16 items had no text; K3 = 1.00 for all of them; K3–text length +0.82.
  Generation from a repository id alone is unlikely to be useful.
* Novelty against K2, split by text availability (`k2_novelty_by_text.py`, outside the
  repository, SHA-256 c978416d33337c6883bdabe8c35bcaa3c34eca7d0b1b1a2ec79b48774cbd9317; run on
  the same snapshot, unchanged):
  * all ENRICHED: n = 30, rho −0.347 [−0.63 … −0.02];
  * shown with text: n = 24, rho −0.575 [−0.72 … −0.27];
  * title only: n = 6, rho +0.655, too small to conclude anything.

  The objection that title-only items distorted the result was tested and is not supported:
  among items shown with text the negative relation is stronger and its interval excludes 0. The
  other caveat stands: K2 reflects the rater's own knowledge of the field, not ground truth.
  Recorded as an observation only; no TD and no calibration action. Novelty is written by the
  embedding stage and read by no ranking, filtering or selection step (AD-19).
* For a future evaluation round: add a "cannot judge" option, so that missing information is not
  recorded as a low score.

TD-025 → Removed Debt. TD-026 recorded. TD-022 gains the github_trending figure.