# AI Research Intelligence Platform (ARIP)
# Final Architecture — Implementation Ready
**Version:** 1.0.0-final  
**Status:** FROZEN — Implementation Approved  
**Date:** 2026-07-18  
**Supersedes:** SDS v0.1.0-draft + Architecture Review

> This document resolves all Critical and High findings from the independent architecture review. It is the authoritative reference for implementation. Architectural changes after this point require explicit written justification and approval against the frozen decisions listed in Section 9.

---

# Table of Contents

1. [Critical Issue Resolutions](#1-critical-issue-resolutions)
2. [Simplification Pass — What Changed and Why](#2-simplification-pass--what-changed-and-why)
3. [Complete State Machine](#3-complete-state-machine)
4. [Revised Database Schema](#4-revised-database-schema)
5. [Subsystem Design Validation](#5-subsystem-design-validation)
6. [Revised Project Structure](#6-revised-project-structure)
7. [Performance Strategy](#7-performance-strategy)
8. [Development Roadmap with Quality Gates](#8-development-roadmap-with-quality-gates)
9. [Architecture Freeze](#9-architecture-freeze)
10. [Go / No-Go Decision](#10-go--no-go-decision)
11. [Phase 0 — Project Bootstrap](#11-phase-0--project-bootstrap)

---

# 1. Critical Issue Resolutions

## 1.1 Resolution: AsyncIO — Decision is SYNCHRONOUS

**Decision: Fully synchronous Python. AsyncIO is not used anywhere in ARIP v1.**

### Rationale

The architecture review correctly identified that the combination of asyncio + sequential LLM inference + synchronous SQLAlchemy created undefined behavior at the async/sync boundary, with no enforcement mechanism preventing concurrent VRAM access.

The workload profile does not justify asyncio:
- One operator. One process. One GPU.
- LLM inference is the dominant bottleneck and is inherently sequential.
- HTTP source fetching is I/O-bound but runs at most every few hours. The latency difference between sync and async fetch across 6 sources is under 5 seconds per pipeline run — irrelevant against a 30-minute generation pass.
- The only genuine concurrency requirement is the Telegram review polling loop, which must be active while the pipeline scheduler is idle. This is solved with a single background thread — a 5-line solution that requires no asyncio.

Asyncio would be the correct choice if ARIP were a web server handling concurrent users, or a real-time ingestion pipeline with tight latency requirements. It is neither.

### What This Means in Practice

| Concern | Solution |
|---------|----------|
| HTTP source fetching | `httpx` sync client (or `requests`) |
| LLM inference | Synchronous PyTorch forward pass |
| Database access | Synchronous SQLAlchemy ORM |
| Scheduling | `schedule` library with `time.sleep()` main loop |
| Telegram review polling | Single daemon `threading.Thread` running `bot.run_polling()` |
| No event loop | No `asyncio.run()`, no `await`, no `async def` anywhere |

The background thread for Telegram polling is the only concurrency in the system. It communicates with the main thread via a thread-safe queue (`queue.Queue`). The main pipeline loop checks this queue at the start of each run. This is simple, debuggable, and safe.

```
Main Thread                         Telegram Thread
──────────────────────────────────  ──────────────────────────────
schedule.run_pending() [loop]       bot.run_polling() [blocking]
  │                                   │
  ├─ pipeline.run()                    ├─ on_callback_query()
  │   ├─ collect()                     │    └─ decision_queue.put(decision)
  │   ├─ rank()                        │
  │   ├─ deduplicate()                 └─ (waits for next callback)
  │   ├─ generate()
  │   ├─ check_review_queue()  ←──── decision_queue.get_nowait()
  │   └─ publish()
  └─ sleep(60)
```

---

## 1.2 Resolution: Plugin Discovery — Decision is EXPLICIT REGISTRATION

**Decision: All plugins are explicitly registered via imports in their package's `__init__.py`. The registry scans `BaseClass.__subclasses__()` after those imports have run.**

### The Problem Restated

Both `__subclasses__()` discovery and decorator-based registration require the module to be imported before registration occurs. Python does not auto-import package modules. Any approach that relies on "automatic" discovery either breaks silently or requires fragile glob-import of all files in a directory.

### The Solution

Each plugin package has a `__init__.py` that explicitly imports all implementations:

```python
# arip/sources/__init__.py
from .huggingface_papers import HuggingFacePapersSource
from .huggingface_models import HuggingFaceModelsSource
from .huggingface_spaces import HuggingFaceSpacesSource
from .arxiv import ArXivSource
from .github_trending import GitHubTrendingSource
from .papers_with_code import PapersWithCodeSource
```

The `SourceRegistry` is then constructed by scanning `BaseSource.__subclasses__()`:

```python
# SourceRegistry.__init__
self._sources = {
    cls.source_id: cls(config.sources.get(cls.source_id))
    for cls in BaseSource.__subclasses__()
    if config.sources.get(cls.source_id, {}).get("enabled", True)
}
```

### Why This Works

- **Deterministic**: import order is explicit and visible in one file.
- **Debuggable**: add a `print()` to the `__init__.py` and you see exactly what loaded.
- **Adding a new source**: create the file, add one import line to `__init__.py`. Nothing else changes.
- **No magic**: any Python developer reading the code understands what's happening immediately.

The `get_config_schema()` method is changed to a `@classmethod` as identified in the review, resolving the chicken-and-egg instantiation problem.

---

## 1.3 Resolution: State Machine — Complete and Reduced to 12 States

**Decision: 12 states. Complete transition matrix. Single `FAILED` state with `failed_at_stage` column for context. `PARTIALLY_PUBLISHED` eliminated.**

The full specification is in Section 3.

---

# 2. Simplification Pass — What Changed and Why

Every component was evaluated against YAGNI, KISS, and the project's stated priorities. The following changes are permanent.

## 2.1 Removed: Generic `BaseRepository[T]`

**Before:** Abstract generic repository base class with type parameters.  
**After:** Plain Python classes per entity type. No inheritance.

`ItemRepository`, `ContentRepository`, `PublishingRepository` are standalone classes. They share no base. Each has exactly the methods its callers need. Testable by passing a different SQLAlchemy session (pointing at `:memory:` SQLite).

## 2.2 Removed: DI Container (`container.py`)

**Before:** Central `AppContainer` service locator passed through the system.  
**After:** Constructor injection at the application entrypoint (`main.py`).

All objects are constructed and wired in `main.py` (under 60 lines of straightforward factory code). Each class receives its dependencies via `__init__` parameters. No object reaches into a global container. Testing a class means passing different objects to its constructor.

## 2.3 Removed: Per-Source YAML Config Fragments

**Before:** `config/sources/*.yaml` auto-loaded and deep-merged.  
**After:** Single `config/settings.yaml`. One `AppSettings` Pydantic model. All source configs are typed sub-models within it.

Merge semantics are no longer ambiguous. Pydantic validates all source configs at startup. Adding a source adds a typed block to `AppSettings`. No glob-loading, no YAML merge logic to write or debug.

## 2.4 Removed: `embeddings` Database Table

**Before:** Vector blobs stored in SQLite alongside an ANN index file, creating a two-store synchronization requirement.  
**After:** Two columns on the `items` table (`embedding_computed_at`, `embedding_model_name`). The ANN index file is the sole vector store.

Rationale: SQLite cannot query vectors. The table existed only for index rebuilding on restart — which is handled more simply by querying `items WHERE embedding_computed_at IS NOT NULL` and re-embedding anything missing from the index.

## 2.5 Removed: `item_metadata` Separate Table

**Before:** One-to-one `item_metadata` table joined to `items`.  
**After:** Metadata fields merged directly into `items` table.

A one-to-one join on a single-user SQLite hobby project is pure overhead with no normalization benefit. Every access required a join. Now metadata is direct column access.

## 2.6 Removed: `validate_content()` from `BasePublisher`

**Before:** Per-publisher validation method, creating a second validation pass after the pipeline's Validation stage.  
**After:** Validation is performed once, in the Validation stage. Publishers format and post. Character limits are class-level constants on each publisher, not a method call.

## 2.7 Removed: `get_vram_footprint_gb()` and `supports_language()` from `BaseLLM`

Both were pseudo-safety mechanisms that didn't actually provide safety. `get_vram_footprint_gb()` was a heuristic estimate that could be wrong in both directions. `supports_language()` would always return `True` for general-purpose open-weight models. Both removed. VRAM management is documented in the config reference and enforced by catching `torch.cuda.OutOfMemoryError` explicitly.

## 2.8 Removed: Prompt Template Version Directories

**Before:** `config/prompts/v1/`, `config/prompts/v2/` — versioned directories.  
**After:** `config/prompts/` — flat directory, one file per language per use case.

Git is the version history. Prompt versioning via directories is YAGNI for a solo project. The `generated_content` table stores the full rendered prompt (so you can always see exactly what was sent to the model), which is the only audit capability actually needed.

## 2.9 Replaced: `str.format_map()` → Jinja2

All prompt template rendering uses Jinja2. Handles curly braces in content, `None` fields gracefully, conditional blocks, and whitespace control. Not negotiable given that real paper titles and abstracts routinely contain `{}` characters.

## 2.10 Replaced: APScheduler → `schedule` Library

`schedule` is a 600-line single-file library. Its API is: `schedule.every(6).hours.do(run_pipeline)`. No daemon threads, no missed jobs policy to configure, no version API changes between major releases. The main loop is `while True: schedule.run_pending(); time.sleep(60)`. This is more than sufficient for a hobby project running one job every 6 hours.

## 2.11 Replaced: Explicit `load()`/`unload()` → Context Manager

`BaseLLM` now implements `__enter__`/`__exit__`. The pipeline generator stage uses:

```python
with self.llm_registry.get_backend() as llm:
    for item in items_to_generate:
        result = llm.generate(prompt, params)
```

This guarantees: model unloads on exit regardless of exceptions, VRAM is released before the next stage, and it is impossible to call `generate()` before `load()`.

## 2.12 Flattened: Folder Structure

Deep sub-packages (`arip/core/interfaces/`, `arip/dedup/`, `arip/ranking/`) are merged into flat modules at the `arip/` level. Detailed in Section 6.

## 2.13 Renamed: `ValidationError` → `ContentValidationError`

Avoids collision with `pydantic.ValidationError`. Applied consistently across the exception hierarchy.

## 2.14 Simplified: Exception Hierarchy

Before: 15+ exception types.  
After: 7 exception types. Sub-classes only where the caller would handle them differently.

```
ARIPError (base)
├── ConfigError
├── SourceError          (fetch failures, rate limits, auth — all handled the same way: retry)
├── LLMError             (load failures, generation failures, OOM)
├── EmbeddingError
├── ContentValidationError
├── ReviewError
└── PublisherError
```

`NormalizationError` eliminated — normalization failures are logged and the item moves to `FAILED`. No caller has reason to catch `NormalizationError` differently from other processing errors.

---

# 3. Complete State Machine

## 3.1 State Definitions

| # | State | Category | Description |
|---|-------|----------|-------------|
| 1 | `COLLECTED` | Active | Raw payload stored; normalization not yet run |
| 2 | `RANKED` | Active | Normalized + scored; passed threshold |
| 3 | `EMBEDDED` | Active | Embedding computed; passed semantic dedup |
| 4 | `ENRICHED` | Active | Metadata extracted; ready for generation |
| 5 | `GENERATED` | Active | LLM content produced and validated |
| 6 | `PENDING_REVIEW` | Active | Submitted to human reviewer |
| 7 | `APPROVED` | Active | Approved (human or auto); ready to publish |
| 8 | `PUBLISHED` | Active | Successfully published to ≥1 publisher |
| 9 | `ARCHIVED` | Terminal | Pipeline complete; no further transitions |
| 10 | `DUPLICATE` | Terminal | Exact or semantic duplicate; no action taken |
| 11 | `FILTERED` | Terminal | Below importance threshold; no action taken |
| 12 | `FAILED` | Terminal* | Stage failure; `failed_at_stage` column records where |

`*` `FAILED`, `REJECTED`, `DUPLICATE`, and `FILTERED` can be manually re-queued via CLI. All other terminal states are strictly terminal.

**Note on REJECTED:** Rejected items are rare enough that no dedicated `REJECTED` state is justified. A rejected item transitions to `FAILED` with `failed_at_stage = 'REVIEW'` and `failure_reason = 'Rejected by reviewer: {note}'`. The `review_records` table preserves the full decision history.

## 3.2 State Transition Matrix

Each cell contains the **triggering event**. Empty cells are **forbidden** — any attempt to make this transition raises `InvalidTransitionError`.

| FROM ↓ \ TO → | COLLECTED | RANKED | EMBEDDED | ENRICHED | GENERATED | PENDING_REVIEW | APPROVED | PUBLISHED | ARCHIVED | DUPLICATE | FILTERED | FAILED |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **COLLECTED** | — | norm_ok | | | | | | | | | norm_ok, score_low | norm_error |
| **RANKED** | | — | embed_ok | | | | | | | | | embed_error |
| **EMBEDDED** | | | — | dedup_pass | | | | | | dedup_hit | | embed_error |
| **ENRICHED** | | | | — | gen_ok | | | | | | | gen_max_retry |
| **GENERATED** | | | | | — | review_enabled | review_disabled | | | | | |
| **PENDING_REVIEW** | | | | regen_request | | — | reviewer_approve | | | | | reviewer_reject / review_timeout |
| **APPROVED** | | | | | | | — | publish_ok | | | | publish_max_retry |
| **PUBLISHED** | | | | | | | | — | archive_job | | | |
| **ARCHIVED** | | | | | | | | | — | | | |
| **DUPLICATE** | manual_requeue | | | | | | | | | — | | |
| **FILTERED** | manual_requeue | | | | | | | | | | — | |
| **FAILED** | manual_requeue | | | manual_requeue | | | manual_requeue | | | | | — |

## 3.3 Transition Specifications

Each transition is fully defined below.

---

### `COLLECTED → RANKED`
**Trigger:** `norm_ok` — normalization succeeds AND importance score ≥ threshold  
**Guard:** `item.importance_score >= config.ranking.min_score`  
**Action:** Set `importance_score`, `signal_breakdown` JSON, `normalized_at`  
**On failure:** → `FAILED` with `failed_at_stage='NORMALIZATION'`

### `COLLECTED → FILTERED`
**Trigger:** `norm_ok, score_low` — normalization succeeds AND importance score < threshold  
**Guard:** `item.importance_score < config.ranking.min_score`  
**Action:** Set `importance_score`, `filtered_reason='score_below_threshold'`  
**Note:** Normalization and ranking run in the same pass. A single stage handles COLLECTED → (RANKED | FILTERED | FAILED).

### `COLLECTED → FAILED`
**Trigger:** `norm_error` — unrecoverable normalization error (malformed payload, missing required fields)  
**Action:** Set `failed_at_stage='NORMALIZATION'`, `failure_reason=error_message`, increment `retry_count`  
**Retry policy:** If `retry_count < config.pipeline.max_normalization_retries` (default: 1), leave in COLLECTED for next run. Else → FAILED (terminal).

---

### `RANKED → EMBEDDED`
**Trigger:** `embed_ok` — embedding computed without error  
**Action:** Set `embedding_computed_at`, `embedding_model_name`; add vector to ANN index

### `RANKED → FAILED`
**Trigger:** `embed_error` — embedding backend raises `EmbeddingError`  
**Action:** Set `failed_at_stage='EMBEDDING'`, `failure_reason`  
**Note:** Embedding errors are rare (model load failure). Do not retry automatically.

---

### `EMBEDDED → ENRICHED`
**Trigger:** `dedup_pass` — no duplicate found in ANN index above threshold  
**Action:** Set metadata fields, `enriched_at`

### `EMBEDDED → DUPLICATE`
**Trigger:** `dedup_hit` — cosine similarity to nearest neighbor ≥ `config.dedup.semantic_threshold`  
**Action:** Set `duplicate_of_id` (FK to the surviving item), `is_semantic_duplicate=True`  
**Terminal:** No automatic re-processing.

---

### `ENRICHED → GENERATED`
**Trigger:** `gen_ok` — LLM generation succeeds AND validation passes  
**Action:** Insert `GeneratedContent` row with `is_active=True`; set `generated_at` on item  
**Note:** Validation is part of the generation stage. Max validation retries (default: 2) with different seeds before `gen_max_retry`.

### `ENRICHED → FAILED`
**Trigger:** `gen_max_retry` — max generation retries OR max validation retries exhausted  
**Action:** Set `failed_at_stage='GENERATION'`, `failure_reason`  
**Re-queue target:** When manually re-queued, enters `ENRICHED` (skips collection/ranking/dedup).

---

### `GENERATED → PENDING_REVIEW`
**Trigger:** `review_enabled` — `config.review.enabled = True`  
**Action:** Call `reviewer.submit(item)` → store `tracking_id` in `review_records`; set `review_submitted_at`

### `GENERATED → APPROVED`
**Trigger:** `review_disabled` — `config.review.enabled = False`  
**Action:** Set `approved_at`, `auto_approved=True`

---

### `PENDING_REVIEW → APPROVED`
**Trigger:** `reviewer_approve` — `ReviewDecision.decision = APPROVE` received from decision queue  
**Action:** Update `review_records` with decision; set `approved_at`, `auto_approved=False`

### `PENDING_REVIEW → FAILED`
**Trigger (rejection):** `reviewer_reject` — `ReviewDecision.decision = REJECT`  
**Action:** Set `failed_at_stage='REVIEW'`, `failure_reason='Rejected: {reviewer_note}'`; update `review_records`  
**Re-queue target:** When manually re-queued, enters `ENRICHED` (regenerate) or `COLLECTED` (full restart).

**Trigger (timeout):** `review_timeout` — `datetime.now() - review_submitted_at > config.review.timeout_hours`  
**Action:** Set `failed_at_stage='REVIEW_TIMEOUT'`, `failure_reason='Reviewer timeout'`  
**Checked by:** Hourly background scan in the main loop.

### `PENDING_REVIEW → ENRICHED`
**Trigger:** `regen_request` — `ReviewDecision.decision = REGENERATE`  
**Action:** Set existing `GeneratedContent.is_active = False`; update `review_records` with decision; return item to ENRICHED for a new generation attempt  
**Note:** This is the only transition that goes backward in the pipeline. It is expected and handled by the state machine explicitly.

---

### `APPROVED → PUBLISHED`
**Trigger:** `publish_ok` — at least one publisher succeeds  
**Action:** Insert `PublishingRecord` rows; set `published_at`  
**Note:** If multiple publishers configured and one fails, `PublishingRecord` for the failed publisher has `status=FAILED`. The item still becomes `PUBLISHED`. The failed publisher is retried independently.

### `APPROVED → FAILED`
**Trigger:** `publish_max_retry` — ALL configured publishers have failed after max retries  
**Action:** Set `failed_at_stage='PUBLISHING'`, `failure_reason`  
**Re-queue target:** When manually re-queued, enters `APPROVED` (skips to publishing).

---

### `PUBLISHED → ARCHIVED`
**Trigger:** `archive_job` — archive scan finds items in `PUBLISHED` state  
**Action:** Set `archived_at`; no data is deleted — archiving is a status update only  
**When:** Runs as final step of each pipeline run, or as a standalone pass.

---

### Manual Re-Queue Rules

| From State | `failed_at_stage` | Re-Queue Target |
|------------|-------------------|-----------------|
| `FAILED` | `NORMALIZATION` | `COLLECTED` |
| `FAILED` | `EMBEDDING` | `RANKED` |
| `FAILED` | `GENERATION` | `ENRICHED` |
| `FAILED` | `VALIDATION` | `ENRICHED` |
| `FAILED` | `REVIEW` | `ENRICHED` (regenerate) or `COLLECTED` (restart) |
| `FAILED` | `REVIEW_TIMEOUT` | `GENERATED` (re-submit to reviewer) |
| `FAILED` | `PUBLISHING` | `APPROVED` |
| `FILTERED` | n/a | `COLLECTED` |
| `DUPLICATE` | n/a | `COLLECTED` |

Re-queuing resets `retry_count = 0` and clears `failure_reason`.

## 3.4 State Machine Enforcement

`StateMachine` is a single class with one method:

```
StateMachine.transition(item, target_status, context=None) -> None
```

The allowed transitions are encoded as a `frozenset` of `(from_status, to_status)` tuples. Any attempt to make an unlisted transition raises `InvalidTransitionError` with a message identifying the invalid `from → to` pair. This is tested exhaustively in the test suite. No pipeline stage may call `item.status = X` directly — all mutations go through `StateMachine.transition()`.

---

# 4. Revised Database Schema

## 4.1 Design Principles

- SQLite with WAL mode enabled (`PRAGMA journal_mode=WAL`)
- SQLAlchemy 2.x synchronous ORM
- Alembic for migrations (from the first migration — retrofitting Alembic is more painful than starting with it)
- Integer primary keys for `items` (faster SQLite indexing than UUID strings); UUID stored as a separate `external_uuid` column for public API use
- All required indexes defined explicitly in migration scripts

## 4.2 Table: `items`

The central table. One row per discovered item at any stage of its lifecycle.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | INTEGER PK | No | autoincrement | Fast SQLite PK |
| `uuid` | VARCHAR(36) | No | uuid4() | Stable external identifier |
| `source_id` | VARCHAR(64) | No | | e.g. `"arxiv"` |
| `source_type` | VARCHAR(16) | No | | `PAPER` / `MODEL` / `SPACE` / `REPO` |
| `external_id` | VARCHAR(256) | No | | Source-native ID (arxiv ID, GitHub slug) |
| `content_hash` | VARCHAR(64) | No | | SHA-256 of `source_id + external_id + title[:200]` |
| `status` | VARCHAR(24) | No | `COLLECTED` | Current state machine state |
| `failed_at_stage` | VARCHAR(32) | Yes | NULL | Populated only when status=FAILED |
| `failure_reason` | TEXT | Yes | NULL | Error message or rejection note |
| `retry_count` | INTEGER | No | 0 | Total retry attempts across all stages |
| `duplicate_of_id` | INTEGER FK | Yes | NULL | References `items.id` of the surviving item |
| `is_semantic_duplicate` | BOOLEAN | No | False | True if dedup was semantic (not exact) |
| `importance_score` | REAL | Yes | NULL | Composite ranking score [0.0–1.0] |
| `signal_breakdown` | TEXT (JSON) | Yes | NULL | Per-signal scores for debugging |
| `language` | VARCHAR(8) | No | `EN` | Target generation language |
| `auto_approved` | BOOLEAN | No | False | True if review was skipped |
| `title` | TEXT | Yes | NULL | Extracted metadata |
| `authors` | TEXT (JSON) | Yes | NULL | List[str] |
| `institutions` | TEXT (JSON) | Yes | NULL | List[str] |
| `abstract` | TEXT | Yes | NULL | |
| `primary_url` | TEXT | Yes | NULL | |
| `additional_urls` | TEXT (JSON) | Yes | NULL | |
| `published_date` | DATE | Yes | NULL | |
| `topics` | TEXT (JSON) | Yes | NULL | List[str] |
| `source_signals` | TEXT (JSON) | Yes | NULL | Stars, downloads, likes, citations |
| `embedding_computed_at` | TIMESTAMP | Yes | NULL | NULL = not yet embedded |
| `embedding_model_name` | VARCHAR(128) | Yes | NULL | |
| `collected_at` | TIMESTAMP | No | now() | |
| `updated_at` | TIMESTAMP | No | now() | Updated on every state transition |
| `normalized_at` | TIMESTAMP | Yes | NULL | |
| `ranked_at` | TIMESTAMP | Yes | NULL | |
| `enriched_at` | TIMESTAMP | Yes | NULL | |
| `generated_at` | TIMESTAMP | Yes | NULL | |
| `review_submitted_at` | TIMESTAMP | Yes | NULL | |
| `approved_at` | TIMESTAMP | Yes | NULL | |
| `published_at` | TIMESTAMP | Yes | NULL | |
| `archived_at` | TIMESTAMP | Yes | NULL | |
| `raw_payload` | TEXT (JSON) | No | | Original source API response |

**Indexes:**
- `UNIQUE(source_id, external_id)` — prevents duplicate collection from same source
- `UNIQUE(content_hash)` — exact deduplication lookup
- `INDEX(status)` — primary pipeline query
- `INDEX(collected_at)` — time-range queries
- `INDEX(importance_score)` — ranking queries

## 4.3 Table: `generated_content`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| `id` | INTEGER PK | No | |
| `item_id` | INTEGER FK | No | References `items.id` |
| `language` | VARCHAR(8) | No | |
| `attempt` | INTEGER | No | Generation attempt number (1-based) |
| `is_active` | BOOLEAN | No | True = selected for review/publishing |
| `prompt_template_id` | VARCHAR(64) | No | e.g. `"thread_en"` |
| `full_prompt` | TEXT | No | Complete rendered prompt sent to LLM |
| `raw_output` | TEXT | No | Raw LLM response string |
| `parsed_segments` | TEXT (JSON) | Yes | Structured segments after parsing |
| `full_text` | TEXT | Yes | Assembled final thread text |
| `validation_passed` | BOOLEAN | No | |
| `validation_errors` | TEXT (JSON) | Yes | List of validation error strings |
| `llm_backend_id` | VARCHAR(64) | No | |
| `model_name` | VARCHAR(128) | No | |
| `temperature` | REAL | No | |
| `max_tokens` | INTEGER | No | |
| `seed` | INTEGER | Yes | |
| `generation_latency_ms` | INTEGER | Yes | |
| `generated_at` | TIMESTAMP | No | |

**Indexes:**
- `INDEX(item_id, is_active)` — find the active content for an item
- `INDEX(item_id, attempt)` — find all attempts for an item

## 4.4 Table: `review_records`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| `id` | INTEGER PK | No | |
| `item_id` | INTEGER FK | No | |
| `content_id` | INTEGER FK | No | References `generated_content.id` |
| `reviewer_backend` | VARCHAR(32) | No | e.g. `"telegram"` |
| `tracking_id` | VARCHAR(128) | No | Platform message ID |
| `submitted_at` | TIMESTAMP | No | |
| `decided_at` | TIMESTAMP | Yes | NULL until decision received |
| `decision` | VARCHAR(16) | Yes | `APPROVE` / `REJECT` / `REGENERATE` |
| `reviewer_note` | TEXT | Yes | Optional human note |

**Note on restart idempotency:** On restart, the `RecoveryService` queries `review_records WHERE decided_at IS NULL` to find pending reviews. It does NOT re-submit items — it only re-starts the Telegram polling thread. The existing Telegram messages remain valid. If a message was deleted by the user, it expires naturally via `review_timeout`.

## 4.5 Table: `publishing_records`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| `id` | INTEGER PK | No | |
| `item_id` | INTEGER FK | No | |
| `content_id` | INTEGER FK | No | |
| `publisher_id` | VARCHAR(32) | No | e.g. `"twitter"`, `"dry_run"` |
| `status` | VARCHAR(16) | No | `PUBLISHED` / `FAILED` / `RETRYING` |
| `external_id` | VARCHAR(256) | Yes | Platform post ID |
| `external_url` | TEXT | Yes | |
| `published_at` | TIMESTAMP | Yes | |
| `retry_count` | INTEGER | No | 0 |
| `last_error` | TEXT | Yes | |

**Idempotency check:** Before publishing, query `WHERE item_id=? AND publisher_id=? AND status='PUBLISHED'`. If found, skip. This is the only guard needed.

## 4.6 Table: `pipeline_runs`

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| `id` | INTEGER PK | No | |
| `started_at` | TIMESTAMP | No | |
| `completed_at` | TIMESTAMP | Yes | |
| `status` | VARCHAR(16) | No | `RUNNING` / `COMPLETED` / `FAILED` |
| `stage_metrics` | TEXT (JSON) | Yes | `{stage: {in, out, duration_ms, errors}}` |
| `error_summary` | TEXT | Yes | |

**Concurrency guard:** On startup, query `WHERE status='RUNNING'`. If found, the previous run crashed — set it to `FAILED` before starting a new run. At schedule time, check if a `RUNNING` record exists (defensive: the schedule library is single-threaded, so this shouldn't happen, but it's cheap insurance).

## 4.7 Table: `raw_source_payloads`

Separated from `items` to keep the primary lookup table lean. Written once, never updated.

| Column | Type | Nullable | Notes |
|--------|------|----------|-------|
| `id` | INTEGER PK | No | |
| `item_id` | INTEGER FK | No | |
| `source_id` | VARCHAR(64) | No | |
| `fetch_url` | TEXT | Yes | |
| `payload` | TEXT (JSON) | No | Original API response |
| `fetched_at` | TIMESTAMP | No | |

**Purpose:** Enables replay of the normalization stage without re-fetching. The `items` table no longer carries a `raw_payload` column.

---

# 5. Subsystem Design Validation

Each subsystem is validated for implementation readiness.

---

## 5.1 Scheduler

**Purpose:** Trigger pipeline runs on a configurable schedule; manage the Telegram polling background thread lifetime.

**Responsibilities:**
- Start the Telegram polling background thread on application startup (if review is enabled)
- Register and run the pipeline job via `schedule` library
- Enforce single-instance execution via `pipeline_runs` table check
- Provide a manual trigger path (`python -m arip run` CLI command)

**Dependencies:** `schedule` library, `threading`, `PipelineOrchestrator`, `TelegramPollingThread` (if review enabled)

**Public Interface:**
```
Scheduler.start() -> None          # enters the main loop
Scheduler.trigger_now() -> None    # one-shot manual run
Scheduler.stop() -> None           # graceful shutdown
```

**Lifecycle:** `start()` blocks the main thread. Receives SIGINT/SIGTERM for graceful shutdown (stops the polling thread, waits for any in-progress pipeline run to complete naturally).

**Failure Modes:**
- Schedule library failure: rare; log and retry on next tick
- Polling thread crash: log the exception, restart the thread with exponential backoff (max 3 restarts before giving up and logging CRITICAL)

**Testing Strategy:** Test `PipelineOrchestrator` independently. Test the scheduler's concurrency guard with a mocked DB session. Do not test the `schedule` library itself.

---

## 5.2 Source Registry

**Purpose:** Hold all enabled source plugin instances. Provide them to the collection stage.

**Responsibilities:**
- Import all source modules (via package `__init__.py`)
- Instantiate enabled sources with their config
- Provide `get_active_sources() -> list[BaseSource]`
- Report source health for logging

**Dependencies:** `AppSettings`, all source modules (imported at startup)

**Public Interface:**
```
SourceRegistry(config: AppSettings) -> instance
SourceRegistry.get_active_sources() -> list[BaseSource]
SourceRegistry.get_source(source_id: str) -> BaseSource | None
```

**Lifecycle:** Constructed once at startup in `main.py`. Stateless after construction. Sources are long-lived objects (they hold HTTP client sessions if needed).

**Failure Modes:**
- A source class raises in `__init__`: caught, logged, that source is excluded from `get_active_sources()`. Pipeline continues with remaining sources.
- Config for a source is missing: `BaseSource.__init__` receives `None`; each source must handle gracefully (use defaults or raise `ConfigError` which is caught above).

**Testing Strategy:** Construct `SourceRegistry` with a minimal config fixture. Assert that the correct subset of sources is active.

---

## 5.3 Source Plugins

**Purpose:** Fetch raw items from a specific external data source and return them in a normalized carrier format.

**Responsibilities:**
- HTTP fetching with retry (3 retries, exponential backoff, via `tenacity`)
- Returning `list[RawSourcePayload]`
- Respecting rate limits (per-source configurable delay)
- Reporting its own `source_id`, `source_type`

**Base Interface:**
```
BaseSource (ABC)
├── source_id: ClassVar[str]      # class attribute
├── source_type: ClassVar[SourceType]
├── @classmethod get_config_schema() -> type[BaseModel]
├── fetch() -> list[RawSourcePayload]
└── health_check() -> SourceHealth   # optional, best-effort
```

**RawSourcePayload:**
```
RawSourcePayload
├── source_id: str
├── source_type: SourceType
├── external_id: str
├── raw_data: dict
└── fetched_at: datetime
```

**Lifecycle:** `fetch()` is called once per pipeline run per source. No state is held between calls. HTTP client is created fresh per call (simplicity) or held as instance attribute (performance, Phase 2 decision).

**Failure Modes:**
- Network timeout: caught by `tenacity`, retried. After max retries, logged as WARNING and the source returns empty list. Pipeline continues.
- Auth failure (401): logged as ERROR, returns empty list. Does not retry (credentials won't fix themselves).
- Rate limit (429): logged as WARNING, source sleeps for `Retry-After` if provided, then retries.

**Testing Strategy:** Mock HTTP responses using `respx` (for `httpx`) or `responses` (for `requests`). Test: successful fetch, network timeout, 429 rate limit, malformed response. No network calls in tests.

---

## 5.4 Normalization

**Purpose:** Map a `RawSourcePayload` to the canonical `items` table schema.

**Responsibilities:**
- Extract canonical fields (title, authors, abstract, URL, date, topics)
- Compute `content_hash`
- Assign initial `language` based on config
- Return a dict suitable for `ItemRepository.create()`

**Design:** Normalization is **not a separate class**. It is a method on each `BaseSource` subclass:

```
BaseSource.normalize(payload: RawSourcePayload) -> NormalizedItem
```

Each source knows its own schema best. A central normalizer that knows the schemas of all sources violates single responsibility. This eliminates the standalone Normalization stage as a separate module — it runs as part of the collection stage.

**Failure Modes:** Missing required fields (title, primary URL) → `NormalizationError` caught by collection stage → item marked `FAILED` with `failed_at_stage='NORMALIZATION'`.

**Testing Strategy:** Unit test each source's `normalize()` method with fixture payloads captured from real API responses.

---

## 5.5 Ranking

**Purpose:** Assign a composite importance score to each normalized item.

**Responsibilities:**
- Compute 4 CPU-only signals
- Apply configurable weights
- Return `(score: float, breakdown: dict[str, float])`
- Filter items below threshold

**Signals (no GPU, no ML):**

| Signal | Weight (default) | Computation |
|--------|-----------------|-------------|
| Recency | 0.25 | Exponential decay: `exp(-k × days_elapsed)`. k = 0.15 (configurable). |
| Source Authority | 0.25 | Static per-source weight from config. Defaults: ArXiv=0.85, HF Papers=0.80, PwC=0.75, GitHub=0.65, HF Models=0.55, HF Spaces=0.45 |
| Engagement | 0.35 | Normalized metric per source type. Percentile within the current batch (not all-time). Batch minimum = 1 if only one item. |
| Topic Relevance | 0.15 | Keyword intersection with configurable topic list. Score = `min(matches / 3, 1.0)` (diminishing returns). |

**Novelty signal:** Removed from ranking (resolved the FR-R03 contradiction). Novelty is computed as a byproduct of semantic deduplication — the ANN distance to nearest neighbor is stored in `items.signal_breakdown` but is not used in the ranking score.

**Dependencies:** None (pure Python arithmetic, no imports beyond `math`)

**Failure Modes:** Cannot fail on valid data. Malformed `source_signals` JSON → log warning, use 0.0 for engagement signal.

**Testing Strategy:** Unit test with synthetic items. Assert weight validation (must sum to 1.0 ± 0.001). Test threshold filtering. Fully deterministic; no mocking needed.

---

## 5.6 Embedding Service

**Purpose:** Compute dense vector representations of items for semantic deduplication.

**Responsibilities:**
- Load embedding model on first call (lazy load), from CPU RAM
- Compute embeddings as float32 numpy arrays
- Expose `embed(texts: list[str]) -> np.ndarray`
- Manage model lifecycle (load once, keep in memory during dedup pass, unload after)

**Design Decision: CPU-only.** The embedding model (all-MiniLM-L6-v2, 384-dim, ~90 MB) runs on CPU. The LLM runs on GPU. They must never share VRAM. This is enforced by setting `device='cpu'` in the `SentenceTransformer` constructor — not by convention but by explicit code.

**Base Interface:**
```
BaseEmbedder (ABC)
├── backend_id: ClassVar[str]
├── embedding_dim: int (property)
├── __enter__(self) -> BaseEmbedder    # loads model
├── __exit__(self, ...) -> None        # unloads model
└── embed(texts: list[str]) -> np.ndarray
```

**Lifecycle (context manager):**
```python
with embedder:
    for item in items:
        vector = embedder.embed([item.title + ". " + (item.abstract or "")])
        ann_index.add(item.id, vector)
```

**Failure Modes:**
- Model download failure (first run): `EmbeddingError` — inform user to pre-download model.
- OOM on CPU RAM (extremely unlikely with a 90 MB model): `EmbeddingError`.

**Testing Strategy:** Use a mock embedder that returns random unit vectors of the correct dimension. The deduplication logic is tested against this mock without loading any real model.

---

## 5.7 Duplicate Detection

**Purpose:** Prevent processing and publishing the same or near-identical content twice.

**Responsibilities:**
- Exact deduplication: hash lookup in `items` table
- Semantic deduplication: ANN similarity search against persisted index
- Update ANN index with new items that pass deduplication

**Exact Deduplication:**
`content_hash = SHA256(f"{source_id}:{external_id}:{title[:200]}")` is checked against the `UNIQUE(content_hash)` constraint on `items`. An `IntegrityError` from SQLAlchemy means exact duplicate — item is discarded before insertion.

**Semantic Deduplication:**
Uses `usearch` library (pure Python, no native BLAS dependency issues on Windows). The ANN index is persisted to `data/ann_index.usearch`. On startup, if the file exists, it is loaded. On each run, embeddings for new items are added after the dedup check.

```
SemanticDeduplicator
├── load_index() -> None      # loads from disk or creates new
├── save_index() -> None      # persists after additions
├── is_duplicate(vector, item_id) -> tuple[bool, int | None]
│                             # returns (is_dup, nearest_neighbor_id)
└── add(item_id, vector) -> None
```

**Index Reconciliation on Restart:**
```sql
SELECT id FROM items 
WHERE embedding_computed_at IS NOT NULL 
AND status NOT IN ('DUPLICATE', 'FILTERED', 'FAILED')
```
For each result, check if `item_id` is in the loaded index. If not, re-embed and add. This runs once at startup.

**Failure Modes:**
- ANN index file corrupted: delete file and rebuild from scratch (re-embed all eligible items).
- `usearch` raises on malformed vector: log, skip semantic check, continue with exact check only.

**Testing Strategy:** Test `is_duplicate()` with known similar pairs using mock embeddings (cosine similarity calculated directly). Test index save/load round-trip. Test reconciliation logic with a mocked DB.

---

## 5.8 Prompt Manager

**Purpose:** Load, render, and provide Jinja2 prompt templates for content generation.

**Responsibilities:**
- Load all templates from `config/prompts/` at startup
- Index by `(template_id, language)`
- Render a template given a context dict
- Validate that all required variables are provided before rendering

**Template File Convention:**
- One file per use case per language
- Filename: `{template_id}_{language_code}.j2` (e.g., `thread_en.j2`, `thread_tr.j2`)
- File format: YAML header (template metadata) + `---` separator + Jinja2 template body

**PromptManager Interface:**
```
PromptManager(templates_dir: Path)
├── get_template(template_id: str, language: Language) -> PromptTemplate
└── render(template_id: str, language: Language, context: dict) -> str
```

**None Handling:** Jinja2 templates use `{{ abstract | default("Not provided.") }}` for optional fields. Templates define their own fallback text for missing data. The renderer never injects Python `None` as a string.

**Jinja2 Safety:** The Jinja2 environment uses `jinja2.Sandbox Environment` to prevent template injection attacks from source data containing Jinja2 syntax. This is not a security requirement (there is no threat actor) but prevents accidental template breakage.

**Versioning:** Not implemented in v1. If you need to compare the prompt that produced a specific output, the `generated_content.full_prompt` column stores the fully-rendered prompt for every generation. Git history is the prompt version history.

**Failure Modes:**
- Template file not found for requested `(id, language)`: `ConfigError` at startup (not at generation time).
- Missing required context variable: `jinja2.UndefinedError` — caught by generator, logged, item → FAILED.

**Testing Strategy:** Unit test with minimal fixture templates. Test `None` field handling. Test that curly braces in context values do not break rendering.

---

## 5.9 LLM Backend

**Purpose:** Provide a uniform interface to local LLM inference. Manage model loading and VRAM lifecycle.

**Base Interface:**
```
BaseLLM (ABC)
├── backend_id: ClassVar[str]
├── is_loaded: bool (property)
├── __enter__(self) -> BaseLLM
├── __exit__(self, exc_type, exc_val, exc_tb) -> None
├── generate(prompt: str, params: GenerationParams) -> GenerationResult
└── get_model_info() -> dict    # model name, context length, quantization
```

**GenerationParams** (dataclass):
```
temperature: float = 0.7
max_new_tokens: int = 1024
top_p: float = 0.95
repetition_penalty: float = 1.1
seed: int | None = None
stop_sequences: list[str] = field(default_factory=list)
```

**GenerationResult** (dataclass):
```
text: str
prompt_tokens: int
completion_tokens: int
latency_ms: int
stop_reason: str    # "stop_sequence" | "max_tokens" | "eos"
```

**`stop_reason` is required.** Truncated outputs (`stop_reason == "max_tokens"`) are flagged to the validator, which can reject or warn about potentially cut-off content. This was missing from the original SDS and is a real quality issue with local LLMs.

**Context Manager Usage:**
```python
with llm_registry.get_backend() as llm:
    for item in items_to_generate:
        result = llm.generate(rendered_prompt, params)
        # process result...
# model is unloaded here, VRAM is freed
```

**Sequential Enforcement:** The context manager pattern naturally enforces sequentiality — only one `with llm:` block runs at a time in a synchronous system. No explicit lock needed.

**v1 Implementation:** `TransformersBackend` using Hugging Face `transformers` with `BitsAndBytesConfig` for 4-bit quantization. Model configured via `config.llm.model_name`.

**Failure Modes:**
- `torch.cuda.OutOfMemoryError`: caught in `__enter__`, raises `LLMError("VRAM insufficient for {model_name}. Try a smaller or more quantized model.")`. Clear, actionable message.
- Inference error: caught in `generate()`, raises `LLMError`. Caller retries with different seed.
- `max_tokens` truncation: returned via `stop_reason`, not an error.

**Testing Strategy:** Implement a `StubLLM` backend that returns configurable canned responses without loading any model. All pipeline tests use `StubLLM`. The real backend is integration-tested separately against a small model (~1B parameters).

---

## 5.10 Content Generator

**Purpose:** Orchestrate the full generation pass for a single item: template rendering → LLM call → output parsing → validation.

**Responsibilities:**
- Retrieve the `PromptTemplate` for the item's language and type
- Build the Jinja2 context from item metadata
- Call `llm.generate()` with configured params
- Parse the raw output into structured `ContentSegment` list
- Invoke `ContentValidator`
- On validation failure: retry with incremented seed (up to `config.generation.max_retries` times, default: 2)
- Persist `GeneratedContent` row

**Interface:**
```
ContentGenerator(llm: BaseLLM, prompt_manager: PromptManager, 
                 validator: ContentValidator, content_repo: ContentRepository)
├── generate(item: Item) -> GeneratedContent | None
```

Returns `None` when all retries exhausted (caller marks item FAILED).

**Output Parsing:** The generator instructs the LLM to produce structured output using a defined marker format (e.g., `[HOOK]`, `[PROBLEM]`, etc.). The parser splits on these markers. If parsing fails (LLM ignored the format), the full output is stored in `raw_output` and `parsed_segments` is `None`. The validator checks for `parsed_segments is None` and marks as invalid.

**Failure Modes:**
- Parse failure: retry (LLM may comply on next attempt with different seed).
- Validation failure: retry.
- Max retries: return `None` → caller marks FAILED.
- `LLMError`: propagate up, item marked FAILED (no retry — hardware/model issue).

**Testing Strategy:** Use `StubLLM` with pre-configured outputs (valid, invalid-format, and edge-case outputs). Test retry logic. Test that all retries produce new `GeneratedContent` rows with incrementing `attempt` values.

---

## 5.11 Content Validator

**Purpose:** Check that generated content meets quality and format requirements before human review or publishing.

**Responsibilities (in order):**

| Check | Rule | On Failure |
|-------|------|-----------|
| Segment completeness | All 6 required segment roles present | Fail |
| Source citation | `primary_url` appears in the SOURCE segment | Fail |
| Min length | Each segment ≥ 30 characters | Fail |
| Max length | Full text ≤ configurable limit (default: 3000 chars) | Fail |
| Truncation check | `stop_reason != "max_tokens"` | Warn (not fail) |
| Prohibited phrases | No phrases from configurable blocklist | Fail |
| Hallucination risk | Numeric claims (percentages, benchmark numbers) not present in source metadata | Warn (logged, not fail) |

**Design Note on Hallucination Check:** The validator cannot prevent hallucination, only flag it. The check regex-matches for numeric patterns (e.g., `\d+\.?\d*\%`, `\d+x improvement`) and verifies they appear in `item.source_signals` or `item.abstract`. If a number appears in the generated content but not in the source material, it is logged as a WARNING and the `validation_errors` list includes a human-readable note. The item is not failed on this check — it is flagged for reviewer attention.

**Interface:**
```
ContentValidator(config: ValidationConfig)
└── validate(content: GeneratedContent, item: Item) -> ValidationResult

ValidationResult
├── passed: bool
├── errors: list[str]
└── warnings: list[str]
```

**Testing Strategy:** Test each check independently with fixture content strings. Use parameterized tests for edge cases (minimum length, exactly-at-limit, exceeds-limit).

---

## 5.12 Human Review

**Purpose:** Submit generated content for asynchronous human review via Telegram. Process decisions from a background thread.

**Architecture:**

```
Main Thread                            Telegram Thread
─────────────────────────────────      ─────────────────────────────────
ReviewStage.process(items)             TelegramPollingThread (daemon)
  │                                      │
  ├─ for each GENERATED item:            ├─ bot.run_polling()
  │    reviewer.submit(item)             │    (blocks; internal select loop)
  │    item → PENDING_REVIEW             │
  │                                      ├─ on_callback_query(update):
  │                                      │    decision = parse_callback(update)
  ├─ check_decision_queue():             │    decision_queue.put(decision)
  │    decisions = queue.get_nowait()    │
  │    for d in decisions:               └─ (next callback)
  │       state_machine.transition(
  │         item, d.new_status)
```

**`ReviewDecision` (dataclass):**
```
item_id: int
content_id: int
decision: Literal["APPROVE", "REJECT", "REGENERATE"]
reviewer_note: str | None
tracking_id: str
decided_at: datetime
```

**`BaseReviewer` Interface:**
```
BaseReviewer (ABC)
├── reviewer_id: ClassVar[str]
├── start(decision_queue: queue.Queue) -> None  # starts background activity
├── stop() -> None                              # graceful shutdown
├── submit(item: Item, content: GeneratedContent) -> str  # returns tracking_id
└── format_preview(item: Item, content: GeneratedContent) -> str
```

**Telegram Message Format:**
```
📄 [PAPER] Title here
━━━━━━━━━━━━━━━━━━━━
Score: 0.82  |  ArXiv  |  EN
━━━━━━━━━━━━━━━━━━━━
🎯 HOOK: ...
❓ PROBLEM: ...
💡 SOLUTION: ...
📊 IMPACT: ...
✅ TAKEAWAY: ...
🔗 SOURCE: https://...
━━━━━━━━━━━━━━━━━━━━
[✅ Approve] [❌ Reject] [🔄 Regenerate]
```

Callback data format: `{decision}:{item_id}:{content_id}:{tracking_id}` — all needed context in the callback payload, no state stored in the bot.

**Restart Safety:** On restart, `ReviewStage.on_startup()` queries `items WHERE status='PENDING_REVIEW'`. For each, it queries the corresponding `review_records` row to get the `tracking_id`. The Telegram polling thread starts normally and will receive any future callbacks for those existing messages. No messages are re-sent. If the original message was deleted (e.g., bot was removed from chat), the callback will never arrive and the item will timeout naturally.

**Timeout Check:** Runs at the start of each pipeline run: `items WHERE status='PENDING_REVIEW' AND review_submitted_at < now() - interval(config.review.timeout_hours)`. Each timed-out item is transitioned to FAILED via the state machine.

**Auto-Approve Mode:** When `config.review.enabled = False`, the `ReviewStage` immediately transitions `GENERATED → APPROVED` for every item. No Telegram thread is started.

**Testing Strategy:** Test with a `MockReviewer` that immediately puts decisions into the queue. Test timeout logic with a mocked datetime. Test that `check_decision_queue()` handles an empty queue gracefully.

---

## 5.13 Publisher

**Purpose:** Format and deliver approved content to external platforms.

**Base Interface:**
```
BasePublisher (ABC)
├── publisher_id: ClassVar[str]
├── platform: ClassVar[str]
├── CHARACTER_LIMIT: ClassVar[int]    # class constant, not a method
├── publish(content: GeneratedContent, item: Item) -> PublishingResult
└── health_check() -> bool            # optional; used by startup check
```

**`PublishingResult` (dataclass):**
```
publisher_id: str
status: Literal["PUBLISHED", "FAILED"]
external_id: str | None
external_url: str | None
error_message: str | None
published_at: datetime
```

**Idempotency:** Before calling `publish()`, the publish stage checks `publishing_repo.find(item_id, publisher_id, status='PUBLISHED')`. If found, skips. This is the authoritative idempotency guard.

**v1 Publishers:**
- `TwitterPublisher`: Uses `tweepy`. Posts thread as reply chain. Root tweet ID = `external_id`.
- `DryRunPublisher`: Writes formatted content to `data/dry_run_output.jsonl`. Same `PublishingResult` structure. Same `PublishingRecord` in the database (with `publisher_id='dry_run'`). Fully exercises the idempotency check.

**Retry Mechanism:** The publish stage maintains per-publisher retry state in `publishing_records`. Retry logic:
- On failure: `retry_count += 1`, `status = 'RETRYING'`
- If `retry_count >= config.publishing.max_retries` (default: 3): `status = 'FAILED'`
- Retrying publishers are attempted again on the *next pipeline run*, not immediately (avoids blocking the current run)
- If ALL publishers have `status = 'FAILED'`: item → FAILED

**Testing Strategy:** Mock the `tweepy.Client` with `unittest.mock`. Test happy path, rate limit response, network error. Test idempotency (calling publish twice for same item+publisher).

---

## 5.14 Persistence (Repositories)

**Purpose:** Abstract all database access from business logic.

**Repositories (no base class):**

```
ItemRepository(session: Session)
├── create(data: dict) -> Item
├── get_by_id(item_id: int) -> Item | None
├── get_by_content_hash(hash: str) -> Item | None
├── get_by_status(status: str) -> list[Item]
├── get_by_status_and_language(status: str, language: str) -> list[Item]
├── update_status(item_id: int, status: str, **fields) -> None
├── get_pending_review(timeout_hours: int) -> list[Item]
└── get_by_source_and_external_id(source_id: str, external_id: str) -> Item | None

ContentRepository(session: Session)
├── create(data: dict) -> GeneratedContent
├── get_active_for_item(item_id: int) -> GeneratedContent | None
├── deactivate_all_for_item(item_id: int) -> None
└── get_by_item_and_attempt(item_id: int, attempt: int) -> GeneratedContent | None

ReviewRepository(session: Session)
├── create(data: dict) -> ReviewRecord
├── get_pending() -> list[ReviewRecord]
└── record_decision(tracking_id: str, decision: str, note: str | None) -> ReviewRecord | None

PublishingRepository(session: Session)
├── create(data: dict) -> PublishingRecord
├── find_published(item_id: int, publisher_id: str) -> PublishingRecord | None
├── get_retrying() -> list[PublishingRecord]
└── increment_retry(record_id: int, error: str) -> None
```

**Session Management:** Sessions are created per pipeline run (or per logical unit of work), not per repository. The session is passed to repositories at construction time. A single `with db.session_scope() as session:` context manager wraps each pipeline stage.

**SQLite WAL Mode:** Enabled via event listener on the engine:
```python
@event.listens_for(engine, "connect")
def set_wal_mode(conn, _):
    conn.execute("PRAGMA journal_mode=WAL")
```

**Testing Strategy:** All repository tests use SQLite `:memory:` database with Alembic migrations applied. No mocking of the database layer — test against a real (in-memory) SQLite instance.

---

## 5.15 Configuration

**Purpose:** Load, validate, and provide typed access to all application configuration.

**One file. One model. No fragments.**

`config/settings.yaml` contains all non-secret configuration. Secrets come exclusively from environment variables. Pydantic `BaseSettings` merges both sources and validates at startup.

```
AppSettings (BaseSettings)
├── pipeline: PipelineSettings
│   ├── run_interval_hours: int = 6
│   ├── max_items_per_run: int = 50
│   ├── languages: list[str] = ["EN"]
│   └── max_normalization_retries: int = 1
│
├── ranking: RankingSettings
│   ├── min_score: float = 0.35
│   ├── weights: SignalWeights     # recency, authority, engagement, topic (must sum to 1.0)
│   ├── topic_keywords: list[str]
│   └── source_authority: dict[str, float]
│
├── dedup: DedupSettings
│   ├── semantic_threshold: float = 0.92
│   ├── ann_index_path: str = "data/ann_index.usearch"
│   └── top_k: int = 5
│
├── llm: LLMSettings
│   ├── backend: str = "transformers"
│   ├── model_name: str           # REQUIRED — no default
│   └── generation: GenerationParams
│
├── embeddings: EmbeddingSettings
│   ├── backend: str = "sentence_transformers"
│   └── model_name: str = "all-MiniLM-L6-v2"
│
├── review: ReviewSettings
│   ├── enabled: bool = True
│   ├── backend: str = "telegram"
│   ├── timeout_hours: int = 24
│   └── telegram: TelegramSettings   # chat_id from here; token from env
│
├── publishing: PublishingSettings
│   ├── enabled: bool = True
│   └── publishers: list[str] = ["dry_run"]   # "twitter", "dry_run"
│
├── sources: SourcesSettings
│   ├── huggingface_papers: SourceConfig
│   ├── huggingface_models: SourceConfig
│   ├── huggingface_spaces: SourceConfig
│   ├── arxiv: ArxivSourceConfig    # inherits SourceConfig, adds arxiv-specific fields
│   ├── github_trending: SourceConfig
│   └── papers_with_code: SourceConfig
│
├── database: DatabaseSettings
│   └── url: str = "sqlite:///data/arip.db"
│
└── logging: LoggingSettings
    ├── level: str = "INFO"
    ├── log_file: str = "logs/arip.log"
    └── json_format: bool = True
```

**`SourceConfig` base:**
```
SourceConfig
├── enabled: bool = True
└── fetch_interval_hours: int | None = None   # None = use pipeline.run_interval_hours
```

**Secret Convention:** All secrets use the `ARIP_` prefix. Pydantic `BaseSettings` resolves them automatically.
- `ARIP_TELEGRAM_BOT_TOKEN`
- `ARIP_TWITTER_API_KEY`, `ARIP_TWITTER_API_SECRET`
- `ARIP_TWITTER_ACCESS_TOKEN`, `ARIP_TWITTER_ACCESS_SECRET`
- `ARIP_GITHUB_TOKEN` (optional, for higher rate limits)

**Validation:** `@model_validator(mode='after')` on `SignalWeights` asserts weights sum to 1.0 ± 0.001. All validators run at startup. Invalid config → `ConfigError` → process exits with a clear message.

**Testing Strategy:** Test with minimal YAML fixtures. Test that invalid weights raise. Test that missing required fields (e.g., `llm.model_name`) raise. Test env var override of YAML values.

---

## 5.16 Logging

**Purpose:** Provide structured, context-rich logs for debugging and operations.

**Library:** `structlog` with JSON rendering in production, colorized console rendering in development (auto-detected via `sys.stdout.isatty()`).

**Single log destination per environment:**
- Development: colorized stderr
- Production: JSON to `logs/arip.log` (rotating, max 10 MB × 5 backups) + stderr for ERROR+

**Per-item context binding (required pattern for all stage loops):**
```python
with structlog.contextvars.bound_contextvars(
    run_id=run_id,
    stage="generation",
    item_id=item.id,
    source_id=item.source_id,
):
    content = generator.generate(item)
```

The `with` block guarantees context is cleared after each item, preventing context bleed between items in a loop.

**Standard fields on every log line:**
- `timestamp`, `level`, `event`, `run_id`, `stage`

**Per-item fields (when applicable):**
- `item_id`, `source_id`, `item_status`

**Level Policy:**

| Level | When |
|-------|------|
| DEBUG | LLM prompts/outputs, HTTP response bodies, signal scores |
| INFO | Stage start/end, item count summaries, state transitions |
| WARNING | Retries, rate limits, skipped items, near-duplicates, hallucination warnings |
| ERROR | Exceptions, stage failures, API errors |
| CRITICAL | Startup failures, DB unreachable, unrecoverable errors |

DEBUG is off by default (`config.logging.level = "INFO"`). Temporary upgrade to DEBUG for a single run: `ARIP_LOGGING__LEVEL=DEBUG python -m arip run`.

---

# 6. Revised Project Structure

```
arip/                              # Project root
├── pyproject.toml
├── .env.example
├── config/
│   ├── settings.yaml
│   └── prompts/
│       ├── thread_en.j2
│       └── thread_tr.j2
├── data/                          # Runtime data (gitignored)
│   ├── arip.db
│   └── ann_index.usearch
├── logs/                          # Runtime logs (gitignored)
│   └── arip.log
│
├── arip/
│   ├── __init__.py
│   │
│   ├── main.py                    # Entrypoint: wire + schedule
│   ├── container.py               # Constructor injection (factory functions only, ~50 lines)
│   ├── config.py                  # AppSettings Pydantic model
│   ├── interfaces.py              # All ABCs: BaseSource, BaseLLM, BaseEmbedder, BaseReviewer, BasePublisher
│   ├── entities.py                # Dataclasses: RawSourcePayload, GeneratedContent, etc.
│   ├── enums.py                   # ItemStatus, Language, SourceType, SegmentRole
│   ├── exceptions.py              # ARIPError hierarchy (7 types)
│   ├── state_machine.py           # StateMachine with transition enforcement
│   │
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── orchestrator.py        # Drives the stage sequence
│   │   └── stages/
│   │       ├── collect.py         # Collection + normalization + exact dedup
│   │       ├── rank.py            # Ranking + filtering
│   │       ├── embed.py           # Embedding + semantic dedup
│   │       ├── generate.py        # LLM generation + validation
│   │       ├── review.py          # Review submission + decision processing
│   │       ├── publish.py         # Publishing to all enabled publishers
│   │       └── archive.py         # Move PUBLISHED → ARCHIVED
│   │
│   ├── sources/
│   │   ├── __init__.py            # Explicit imports of all source classes
│   │   ├── registry.py            # SourceRegistry
│   │   ├── huggingface_papers.py
│   │   ├── huggingface_models.py
│   │   ├── huggingface_spaces.py
│   │   ├── arxiv.py
│   │   ├── github_trending.py
│   │   └── papers_with_code.py
│   │
│   ├── backends/
│   │   ├── llm/
│   │   │   ├── __init__.py        # Explicit imports
│   │   │   ├── registry.py
│   │   │   ├── transformers_backend.py
│   │   │   ├── vllm_backend.py    # Phase 7+
│   │   │   └── stub_backend.py    # For testing
│   │   └── embeddings/
│   │       ├── __init__.py
│   │       ├── registry.py
│   │       ├── sentence_transformers_backend.py
│   │       └── stub_backend.py
│   │
│   ├── ranking/
│   │   └── scorer.py              # Scorer + SignalWeights
│   │
│   ├── dedup/
│   │   ├── exact.py
│   │   └── semantic.py
│   │
│   ├── generation/
│   │   ├── generator.py
│   │   ├── validator.py
│   │   └── prompt_manager.py
│   │
│   ├── review/
│   │   ├── __init__.py
│   │   ├── registry.py
│   │   ├── telegram_reviewer.py
│   │   └── auto_reviewer.py       # No-op: immediately approves
│   │
│   ├── publishers/
│   │   ├── __init__.py
│   │   ├── registry.py
│   │   ├── twitter_publisher.py
│   │   └── dry_run_publisher.py
│   │
│   └── db/
│       ├── database.py            # Engine + session factory + WAL pragma
│       ├── models.py              # SQLAlchemy ORM models
│       ├── migrations/            # Alembic
│       │   ├── env.py
│       │   └── versions/
│       │       └── 0001_initial_schema.py
│       └── repositories/
│           ├── items.py           # ItemRepository
│           ├── content.py         # ContentRepository
│           ├── reviews.py         # ReviewRepository
│           └── publishing.py      # PublishingRepository
│
└── tests/
    ├── unit/
    │   ├── test_state_machine.py
    │   ├── test_scorer.py
    │   ├── test_validator.py
    │   ├── test_prompt_manager.py
    │   ├── test_exact_dedup.py
    │   └── test_config.py
    ├── integration/
    │   ├── test_pipeline.py       # Stub LLM + in-memory DB + DryRunPublisher
    │   └── test_repositories.py  # Against :memory: SQLite
    └── fixtures/
        ├── sample_payloads/       # Real API response captures
        └── sample_prompts/        # Test Jinja2 templates
```

**Total top-level modules under `arip/`:** 8 files + 7 sub-packages. Manageable. Every file has a clear, single purpose.

---

# 7. Performance Strategy

**Note on VRAM:** The original SDS specified 12 GB (correct for RTX 4070). This prompt specifies 8 GB. The RTX 4070 has 12 GB VRAM; design for 12 GB but document that all recommendations are tested against an 8 GB budget for conservative compatibility with users who have lower VRAM.

## 7.1 Model Loading Strategy

**Rule: Embedding model and LLM are never in memory simultaneously.**

**Phase 1 — Embedding Pass (CPU only):**
```python
with embedder:                           # loads ~90 MB to CPU RAM
    for item in ranked_items:
        vector = embedder.embed([text])
        deduplicator.check_and_add(item.id, vector)
# embedder.__exit__: model unloaded from CPU RAM
```

**Phase 2 — Generation Pass (GPU):**
```python
with llm_registry.get_backend() as llm: # loads model to GPU VRAM
    for item in enriched_items:
        result = llm.generate(prompt, params)
# llm.__exit__: model unloaded, VRAM freed
torch.cuda.empty_cache()                 # explicit cache release
```

**Recommended Model Configuration for 12 GB VRAM:**

| Model | Format | Est. VRAM | Quality |
|-------|--------|-----------|---------|
| Qwen2.5-7B-Instruct | 4-bit (GPTQ/AWQ) | ~4.5 GB | Excellent for this task |
| Llama-3.1-8B-Instruct | 4-bit (GGUF via llama.cpp) | ~5 GB | Very good |
| Mistral-7B-Instruct-v0.3 | 4-bit | ~4.5 GB | Good |
| Phi-3.5-mini-instruct | 4-bit | ~2.5 GB | Adequate, fast |

Start with **Qwen2.5-7B-Instruct 4-bit** as the default recommendation. It has strong multilingual capabilities (important for Turkish support) and fits comfortably in 12 GB.

**For 8 GB VRAM budgets:** Use Phi-3.5-mini-instruct (2.5 GB) or Qwen2.5-3B-Instruct 4-bit (~2 GB). Quality reduction is acceptable for this educational content use case.

## 7.2 Expected Bottlenecks

| Bottleneck | Impact | Mitigation |
|------------|--------|-----------|
| LLM inference (generation pass) | 15–30 min for 50 items at ~20 tok/s | Irreducible; accept it. Increase `min_score` threshold to reduce items. |
| Source HTTP fetching | <30 seconds total for 6 sources | `httpx` with sensible timeouts (10s connect, 30s read). |
| Embedding pass | ~2–5 min for 50 items on CPU | CPU is fine; all-MiniLM-L6-v2 is fast on modern CPU. |
| ANN index update | <1 second for 50 vectors | `usearch` is extremely fast. |
| SQLite writes | Negligible | WAL mode handles concurrent reads. |

## 7.3 Caching Strategy

**v1: No caching.** Sources are fetched fresh every run. Caching would require invalidation logic that is more complex than the cost it saves (HTTP fetches are <30 seconds).

**What IS cached across runs:**
- The ANN index (persisted to disk, loaded at startup)
- The embedding model (kept in memory for the duration of the embedding pass)
- The LLM model (loaded once per generation pass, unloaded after)

## 7.4 Memory Budget

| Component | Memory | Duration |
|-----------|--------|----------|
| Python process baseline | ~200 MB RAM | Always |
| SQLAlchemy + SQLite | ~50 MB RAM | Always |
| Embedding model (all-MiniLM) | ~200 MB RAM | During embed pass |
| usearch ANN index (10k items) | ~25 MB RAM | Always after first run |
| LLM 7B 4-bit | ~4.5 GB VRAM | During generation pass |
| Peak RAM during generation | ~1–2 GB | KV cache |

Total peak RAM (during generation): ~2.5 GB out of 64 GB. Entirely non-constraining.

---

# 8. Development Roadmap with Quality Gates

Each phase ends with a working application. Later phases add capability, not replace prior work.

---

## Phase 0 — Project Bootstrap

**Goal:** A runnable skeleton with validated infrastructure. No pipeline logic.

**Deliverables:**
- `pyproject.toml` with pinned dependencies
- `AppSettings` with full validation
- SQLAlchemy models + Alembic initial migration (all tables)
- `StateMachine` with complete transition matrix
- `ItemRepository` (basic CRUD)
- `structlog` setup
- `.env.example` documenting all secrets

**Quality Gate — BEFORE PHASE 1:**
- [ ] `python -m arip --help` runs without error
- [ ] Config loads and validates from `settings.yaml`
- [ ] `alembic upgrade head` creates all tables
- [ ] `StateMachine` transition matrix tested: all valid transitions pass, all invalid transitions raise `InvalidTransitionError`
- [ ] `ItemRepository.create()` and `get_by_id()` tested against `:memory:` SQLite
- [ ] **NOT YET:** Any pipeline stage, any source, any LLM

---

## Phase 1 — Walking Skeleton

**Goal:** Complete end-to-end pipeline runs with stub components. Proves the orchestration skeleton works.

**Deliverables:**
- `PipelineOrchestrator` driving all stages in sequence
- ONE hardcoded source: `HuggingFacePapersSource` (simplest REST API)
- `CollectStage` (fetch → normalize → exact dedup → persist)
- `RankStage` (stub: score = 1.0 for all items)
- `EmbedStage` (stub embedder: returns random unit vectors)
- `GenerateStage` (stub LLM: returns template content without model)
- `ValidateStage` (passes everything)
- `ReviewStage` (auto-approve mode only)
- `PublishStage` (DryRunPublisher only)
- `ArchiveStage`
- `schedule` loop with single run trigger
- `pipeline_runs` table populated

**Quality Gate — BEFORE PHASE 2:**
- [ ] `python -m arip run` collects real papers from HF Papers
- [ ] All items pass through all stages to `ARCHIVED`
- [ ] `dry_run_output.jsonl` contains formatted content for each item
- [ ] `pipeline_runs` table shows a completed run with stage metrics
- [ ] Running twice: second run skips already-ARCHIVED items (idempotency)
- [ ] A simulated stage failure (inject exception) → item marked FAILED, pipeline continues
- [ ] **NOT YET:** Real ranking, real embeddings, real LLM, Telegram, Twitter

---

## Phase 2 — Real Data Sources

**Goal:** All 6 sources operational with real normalization and exact deduplication.

**Deliverables:**
- All 6 source plugins + `SourceRegistry` with explicit registration
- Real `normalize()` method on each source
- `content_hash` exact deduplication against DB
- Per-source config blocks in `settings.yaml`
- `SourceHealth` logged per run

**Quality Gate — BEFORE PHASE 3:**
- [ ] All 6 sources fetch and normalize real data
- [ ] `SourceRegistry` loads only enabled sources
- [ ] Exact duplicates (same paper from ArXiv + PwC) marked `DUPLICATE`
- [ ] Disabling a source in config removes it from the run
- [ ] Source HTTP failure → item FAILED, other sources continue
- [ ] **NOT YET:** Ranking logic, embeddings, LLM

---

## Phase 3 — Ranking and Semantic Deduplication

**Goal:** Items are ranked by real signals and near-duplicates are detected.

**Deliverables:**
- `Scorer` with all 4 CPU signals and configurable weights
- `SentenceTransformersBackend` (CPU-only, context manager)
- `SemanticDeduplicator` with usearch ANN index
- ANN index persistence and startup reconciliation
- Filtering by `min_score` threshold

**Quality Gate — BEFORE PHASE 4:**
- [ ] Items receive non-trivial importance scores reflecting real signals
- [ ] Low-score items filtered before generation (verify in DB)
- [ ] Near-duplicate papers from different sources detected and marked `DUPLICATE`
- [ ] ANN index persists across runs and loads correctly on restart
- [ ] Ranking scorer tested with synthetic fixtures (all signals, edge cases)
- [ ] Weight validation catches weights that don't sum to 1.0
- [ ] **NOT YET:** Real LLM, Telegram, Twitter

---

## Phase 4 — Local LLM Generation

**Goal:** Real educational content generated by a local model.

**Deliverables:**
- `TransformersBackend` with 4-bit quantization support (context manager)
- `PromptManager` with Jinja2 and English template
- `ContentGenerator` with retry logic
- `ContentValidator` with all checks
- `GeneratedContent` persisted with full prompt and parsed segments

**Quality Gate — BEFORE PHASE 5:**
- [ ] Local LLM generates structured 6-segment content for real items
- [ ] VRAM released after generation pass (verify with `nvidia-smi`)
- [ ] Embedding model is NOT in VRAM during generation pass
- [ ] `stop_reason='max_tokens'` generates a validation warning in logs
- [ ] Generation retry on failed validation produces new `generated_content` row with `attempt=2`
- [ ] Items with max retries exhausted marked FAILED
- [ ] `StubLLM` tests pass independently of any GPU
- [ ] **NOT YET:** Telegram, Twitter

---

## Phase 5 — Human Review

**Goal:** Items submitted to Telegram reviewer; decisions drive state transitions.

**Deliverables:**
- `TelegramReviewer` with `python-telegram-bot` (polling mode)
- `TelegramPollingThread` daemon thread
- `decision_queue` threading integration
- Review timeout scan
- `REGENERATE` decision loop (PENDING_REVIEW → ENRICHED → GENERATED → PENDING_REVIEW)

**Quality Gate — BEFORE PHASE 6:**
- [ ] Item submitted to Telegram with correct format and inline buttons
- [ ] Approve → item transitions to APPROVED
- [ ] Reject → item transitions to FAILED with rejection note
- [ ] Regenerate → new content generated, new review message sent
- [ ] System restart with pending review item: item remains PENDING_REVIEW, no re-submission
- [ ] Review timeout: item transitions to FAILED after configured hours
- [ ] Auto-approve mode (`review.enabled=False`): no Telegram thread started
- [ ] **NOT YET:** Twitter

---

## Phase 6 — Real Publishing

**Goal:** Approved items published to Twitter as threads.

**Deliverables:**
- `TwitterPublisher` with `tweepy` OAuth 1.0a
- Thread posting (reply chain)
- `PublishingRecord` idempotency
- Retry logic for publisher failures

**Quality Gate — BEFORE PHASE 7:**
- [ ] Approved item published as a thread on Twitter
- [ ] `PublishingRecord` created with `external_id` (root tweet ID)
- [ ] Running pipeline twice: second pass does NOT re-publish (idempotency verified)
- [ ] Simulated Twitter 429 → retry on next run, not immediate retry
- [ ] `DryRunPublisher` still works alongside `TwitterPublisher`
- [ ] **NOT YET:** Turkish language, metrics dashboard

---

## Phase 7 — Polish and Hardening

**Goal:** Production-quality reliability, Turkish support, observability.

**Deliverables:**
- Turkish prompt template (`thread_tr.j2`)
- Stage metrics written to `pipeline_runs.stage_metrics`
- CLI commands: `arip status`, `arip requeue <item_id>`, `arip list --status FAILED`
- `RecoveryService` for startup checks (pending reviews, crashed runs)
- `vLLMBackend` (optional, for higher inference throughput)
- Documentation: setup guide, adding-a-source guide, prompt tuning guide

---

# 9. Architecture Freeze

## 9.1 Final Architecture Decisions

These decisions are frozen. Changing them during implementation requires a written justification and explicit revision of this document.

| ID | Decision | Rationale |
|----|----------|-----------|
| AD-01 | **Fully synchronous Python.** No asyncio anywhere. | One operator, sequential LLM, no concurrent users. Asyncio adds complexity with no benefit. |
| AD-02 | **Explicit plugin registration via `__init__.py` imports.** | Deterministic, debuggable, obvious. No auto-discovery magic. |
| AD-03 | **12-state state machine with complete transition matrix.** | Reviewed and specified completely. `StateMachine.transition()` is the only allowed status mutation. |
| AD-04 | **Single `FAILED` state with `failed_at_stage` column.** | Eliminates state explosion while preserving diagnostic information. |
| AD-05 | **LLM and embedder as context managers.** | Guarantees VRAM release on any exit path including exceptions. |
| AD-06 | **Embedding model: CPU-only. LLM: GPU-only. Never simultaneously.** | Hard constraint on 12 GB VRAM. Enforced by code, not by convention. |
| AD-07 | **Jinja2 for prompt templates.** No `str.format_map()`. | Handles curly braces in content, None fields, and conditional blocks. |
| AD-08 | **Single `settings.yaml`, single `AppSettings` Pydantic model.** | No YAML fragments, no merge ambiguity. Full validation at startup. |
| AD-09 | **Telegram polling via background daemon thread + `queue.Queue`.** | The only concurrency in the system. Simple, debuggable, no asyncio. |
| AD-10 | **No generic `BaseRepository[T]`.** Plain classes per entity. | Eliminates abstraction with no real benefit at this scale. |
| AD-11 | **No DI container.** Constructor injection in `container.py` (factory functions). | Simple, readable, testable. Container is pure factory code only. |
| AD-12 | **SQLite + WAL mode + synchronous SQLAlchemy 2.x + Alembic from day one.** | WAL prevents reader/writer contention. Alembic is cheaper to start with than retrofit. |
| AD-13 | **Idempotency via `PublishingRecord` DB check.** | Single authoritative guard. No in-memory state. |
| AD-14 | **`schedule` library for job scheduling.** | Sufficient for one job every N hours. No APScheduler complexity. |
| AD-15 | **No `PARTIALLY_PUBLISHED` item state.** Per-publisher status in `PublishingRecord`. | Publisher-level granularity belongs in publisher records, not item state. |
| AD-16 | **`ContentValidationError` instead of `ValidationError`.** | Avoids collision with `pydantic.ValidationError`. |
| AD-17 | **`raw_source_payloads` in a separate table.** | Keeps `items` lean. Replay capability preserved. |
| AD-18 | **Normalization is a method on `BaseSource`, not a separate stage.** | Each source knows its own schema. A central normalizer would need to know all schemas. |
| AD-19 | **Novelty signal is a byproduct of deduplication, not a ranking signal.** | Resolves FR-R03 contradiction. Ranking uses only CPU arithmetic. |

## 9.2 Deferred Decisions

| Decision | Deferred Until | Reason |
|----------|---------------|--------|
| `vLLMBackend` implementation | Phase 7 | Not needed until generation speed becomes a bottleneck |
| Turkish LLM quality evaluation | Phase 7 | Need to measure quality of base model before deciding on translate-vs-generate approach |
| PostgreSQL migration | If/when SQLite becomes a bottleneck | Won't happen at hobby project scale; Alembic makes it possible |
| Web dashboard | Post-v1 | CLI + Telegram covers all operational needs for v1 |
| Semantic search over archive | Post-v1 | ANN index is already maintained; exposing it is trivial |
| Multi-language per run (EN + TR simultaneously) | Phase 7 | Start with one language, add Turkish once EN pipeline is stable |
| `AirLLMBackend` | Post-v1 | Only needed if target VRAM drops below 8 GB or model requirements exceed 13B |
| Publisher concurrency | Phase 7 | Sequential publishing is adequate for ≤3 publishers and 50 items/run |
| Per-source scheduling | Phase 7 | One global schedule is sufficient until sources have meaningfully different update frequencies |

## 9.3 Known Technical Debt

Accepted intentionally for v1. Must be tracked and addressed before any public release.

| Debt | Accepted Tradeoff | Cleanup Phase |
|------|-------------------|---------------|
| Hallucination check is heuristic regex, not semantic | A real factuality check requires a second LLM pass, doubling inference time. Regex is good enough with human review enabled. | Phase 7 or post-v1 |
| ANN index is a flat file with no versioning | If embedding model changes, the index must be rebuilt manually. No automatic detection of model change. | Phase 7 (add model name check on load) |
| Turkish output quality is untested | Will rely on base model's multilingual capability. May require post-generation translation. | Phase 7 evaluation |
| No retry jitter on publisher backoff | Can cause all retries to land at the same time after a platform outage. Low risk for single-operator hobby project. | Phase 7 |
| `container.py` is not tested | Factory wiring is verified by running the application. No unit test for the container itself. | Phase 7 |
| All pipeline stages run serially in one transaction | A very long pipeline run holds the SQLite WAL log open. Not a problem at 50 items. | If run size scales significantly |

---

# 10. Go / No-Go Decision

## Final Readiness Assessment: 8.5 / 10

The 0.5 below 9 reflects:
- Turkish language quality remains an open empirical question (not a design problem, but a risk)
- The `TransformersBackend` implementation complexity with 4-bit quantization is non-trivial and will surface edge cases during Phase 4

All Critical and High findings from the architecture review have been resolved:

| Finding | Resolution |
|---------|-----------|
| AsyncIO/sync conflict | ✅ Fully synchronous. Background thread for Telegram only. |
| Plugin discovery broken | ✅ Explicit `__init__.py` imports. Deterministic. |
| Incomplete state machine | ✅ 12 states. Complete transition matrix in Section 3. |
| LLM resource management | ✅ Context manager protocol. |
| `str.format_map()` crash risk | ✅ Jinja2 with SandboxedEnvironment. |
| Config merge ambiguity | ✅ Single file, single Pydantic model. |
| Scheduler concurrency | ✅ `pipeline_runs` guard + `schedule` library (single-threaded). |
| Review race condition | ✅ No re-submission on restart. Polling thread resumes. |
| Embedding table redundancy | ✅ Columns on `items`. ANN index is sole vector store. |
| `ValidationError` naming collision | ✅ Renamed to `ContentValidationError`. |
| Novelty signal contradiction | ✅ Moved to deduplication stage as byproduct. |
| DI container is service locator | ✅ Replaced with constructor injection factory functions. |
| Generic `BaseRepository[T]` | ✅ Removed. Plain classes per entity. |
| Logging context bleed | ✅ `bound_contextvars` with block required in all stage loops. |

---

## ✅ GO

The architecture is sufficiently mature to begin implementation.

**Why:**

Every load-bearing decision is now explicit and resolved:

The **concurrency model** is simple: one thread, one loop, one exception (the Telegram daemon thread with a `queue.Queue` handoff). Any Python developer can understand and debug this in 10 minutes.

The **state machine** is complete. The transition matrix in Section 3 covers every event, every failure, every recovery path, and every forbidden transition. `StateMachine.transition()` is the sole mutator of item status. The database schema follows naturally from the state machine without ambiguity.

The **plugin system** is explicit and deterministic. Adding a source is a two-file change: create the plugin class, add one import line to `sources/__init__.py`. The registry loads exactly what is imported, nothing more.

The **VRAM model** is explicit: embedding runs on CPU, LLM runs on GPU, they never overlap, both use context managers that guarantee cleanup on any exit path.

The **development roadmap** produces a working application at every phase boundary. If development halts after Phase 3, you have a system that collects, deduplicates, and ranks AI research without any LLM. If it halts after Phase 4, you have a system that generates educational content into files. Each phase is independently valuable.

There are no unresolved design questions that would require retroactive architectural changes.

---

# 11. Phase 0 — Project Bootstrap

**Exact objectives for the first coding session.** No code is generated here — only precise specification of what will be built and why.

---

## Objective 1: `pyproject.toml` with Pinned Dependencies

Create the project definition with all Phase 0–2 dependencies pinned to specific versions. Dependencies to be pinned at this point:

- `pydantic[settings]` (settings + validation)
- `sqlalchemy` (ORM)
- `alembic` (migrations)
- `structlog` (logging)
- `httpx` (HTTP client for sources)
- `tenacity` (retry logic for sources)
- `python-dotenv` (`.env` file loading in development)
- `schedule` (job scheduling)
- `pytest`, `pytest-cov` (testing)
- `ruff` (linting + formatting)

Dependencies intentionally excluded from Phase 0 (added in later phases):
- `sentence-transformers` (Phase 3)
- `usearch` (Phase 3)
- `transformers`, `torch`, `bitsandbytes` (Phase 4)
- `python-telegram-bot` (Phase 5)
- `tweepy` (Phase 6)
- `jinja2` (Phase 0–1: included, needed for prompt manager even with stub LLM)

**Why:** Pinning dependencies at project start prevents subtle breakage from transitive dependency updates. It also documents exactly what version combination was tested.

---

## Objective 2: `AppSettings` Pydantic Model

Implement the complete `AppSettings` model as specified in Section 5.15. This is not a stub — the full model is implemented in Phase 0 because config validation is the first thing that runs and must be correct before any other code is written.

**Why:** Every subsequent component receives its configuration from `AppSettings`. If the config model changes shape later, all injected code must change. Getting it right in Phase 0 prevents cascading refactors.

Validation tests to write during this objective:
- Valid minimal config loads successfully
- Missing required `llm.model_name` raises `ConfigError` at startup
- Invalid ranking weights (don't sum to 1.0) raise `ConfigError`
- Env var `ARIP_LOGGING__LEVEL=DEBUG` overrides YAML value

---

## Objective 3: SQLAlchemy Models + Alembic Initial Migration

Implement all ORM models corresponding to Section 4's schema:
- `Item` model (all columns as specified)
- `GeneratedContent` model
- `ReviewRecord` model
- `PublishingRecord` model
- `PipelineRun` model
- `RawSourcePayload` model

Create the first Alembic migration (`0001_initial_schema.py`) that creates all tables with all indexes.

**Why Alembic from day one:** Adding Alembic after the first 3 tables exist requires manually reconstructing the initial migration from a live database, which is error-prone and annoying. Starting with Alembic means `alembic upgrade head` is always the canonical setup command.

No migration is run until the next objective (the model definitions must be complete first).

---

## Objective 4: `StateMachine` with Complete Transition Matrix

Implement `StateMachine` as a class with:
- A `frozenset` of `(from_status, to_status)` tuples encoding all valid transitions from Section 3.2
- A `transition(item, target_status, context=None)` method that:
  - Validates the transition is in the frozenset
  - Raises `InvalidTransitionError` if not
  - Updates `item.status` and `item.updated_at` via the `ItemRepository`
  - Logs the transition at INFO level

**All tests for the state machine are written in Phase 0.** This is the most critical piece of infrastructure and must be exhaustively tested before any stage uses it.

Tests to write:
- Every valid transition (every row in the transition matrix): passes without error
- Representative invalid transitions (e.g., `COLLECTED → PUBLISHED`, `ARCHIVED → COLLECTED`): raises `InvalidTransitionError` with informative message
- `manual_requeue` transitions (FAILED → COLLECTED, FILTERED → COLLECTED): correct re-queue targets per `failed_at_stage`

**Why:** The state machine is the backbone. If it has a bug, items can enter forbidden states and the recovery logic will be wrong. Test it completely before building anything that uses it.

---

## Objective 5: `ItemRepository` Basic CRUD

Implement `ItemRepository` with the methods defined in Section 5.14. All methods tested against an in-memory SQLite database with the initial migration applied.

No other repositories in Phase 0. `ContentRepository`, `ReviewRepository`, and `PublishingRepository` are added in the phases that introduce the corresponding pipeline stages.

---

## Objective 6: `structlog` Setup

Implement `setup_logging(config: LoggingSettings) -> None` that configures structlog with:
- JSON renderer in production (`not sys.stdout.isatty()`)
- Colorized console renderer in development (`sys.stdout.isatty()`)
- Rotating file handler writing to `config.log_file`
- Log level from `config.level`

Verify that `structlog.contextvars.bound_contextvars()` clears context correctly after the `with` block exits (write one test for this).

---

## Objective 7: CLI Entrypoint

Implement `python -m arip` with two subcommands:
- `arip run` — triggers one pipeline run (used for manual testing in Phase 1+)
- `arip check-config` — loads and validates config, prints summary, exits

The `run` subcommand in Phase 0 loads the config, initializes the DB, and prints "Pipeline not yet implemented." This is a stub — it is replaced in Phase 1.

**Why:** A working `arip run` command in Phase 0 means every subsequent phase can be tested by running one command. The CLI entrypoint is infrastructure, not business logic.

---

## Phase 0 Definition of Done

Phase 0 is complete when ALL of the following are true:

- [ ] `pip install -e .` succeeds with no errors
- [ ] `python -m arip check-config` loads `config/settings.yaml` and prints a valid summary
- [ ] `alembic upgrade head` creates all 6 tables in a fresh database
- [ ] `pytest tests/unit/` passes with 100% coverage of `state_machine.py`, `config.py`, and `repositories/items.py`
- [ ] All 12 valid state groups and representative invalid transitions are tested
- [ ] `python -m arip run` exits cleanly with "Pipeline not yet implemented"
- [ ] `ruff check .` reports no linting errors
- [ ] `.env.example` documents all required environment variables

Phase 1 begins immediately after this gate is cleared.

---

*End of Final Architecture — ARIP v1.0.0*  
*Status: FROZEN — Implementation Approved*  
*Next: Implement Phase 0 as specified in Section 11*