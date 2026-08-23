# Project Log

## Current Status

Current Phase: Phase 1
Current Batch: Batch 5 (Completed)
Next Batch: Batch 7 (Batch 6 deferred)
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

- Batch 7
- Integration

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

`<batch6-commit-hash>`

Message:

`docs(batch6): defer Papers With Code source; no public API available`

## Next

Batch 7

* Integration
