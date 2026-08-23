# Technical Debt

Tracked debt across all batches. Each item records why it was accepted and
what would remove it.

| ID | Issue | Why Accepted | Future Action |
|----|-------|--------------|---------------|
| TD-001 | respx 0.21.1 bug #277 — `mock.get()` never matches (bytes/str method mismatch) | Pinned version; blocks all source HTTP tests | Upgrade to respx >= 0.22.0, replace `url__startswith=` with `mock.get()`, delete the notice in `tests/unit/sources/conftest.py` |
| TD-002 | 13 ruff `W292` / `W293` errors across 7 files: `arip/__init__.py`, the three `huggingface_*.py` sources, and their three test files | Pre-existing from Batch 3; unrelated to later batches | `ruff check --fix` in a standalone cleanup commit |
| TD-003 | `abstract` is always `None` for HF Models and HF Spaces | List endpoints return no description; fetching the model/space card costs one extra HTTP request per item | Optional enrichment pass if generation quality proves insufficient |
| TD-004 | Model `title` is the repo ID (`org/name`), not a human-readable name | The models list endpoint exposes no display name | Resolve from the model card alongside TD-003 |
| TD-005 | HF Models / Spaces fetch limit and sort key are hardcoded constants | See D-005 — SDS types both as plain `SourceConfig` | Requires an SDS amendment to become configurable |
| TD-006 | `ruff format` is not enforced; 14 files would be reformatted | Project gate is `ruff check` only (D-004 in the older numbering) | Decide whether to adopt `ruff format` project-wide, then reformat in one commit |
| TD-007 | `github_trending` reads `ARIP_GITHUB_TOKEN` from `os.environ`, bypassing `AppSettings` | Required by D-006 because the existing registry contract cannot carry top-level secrets | Revisit if a future SDS revision allows plugins to receive application-level secrets |
| TD-008 | `.env.example` cites a 60 req/hour GitHub limit; this refers to the core API, while the Search API has different limits | Documentation-only inaccuracy; unrelated to Batch 5 implementation | Correct in a separate housekeeping commit |
| TD-009 | `papers_with_code` is declared in the SDS but unimplemented; only 5 of 6 sources are active | The current Papers With Code service publishes no documented public read API (see PROJECT_LOG Batch 6) | Implement Batch 6 when an official API is published; SDS §8 L1494 and L1509 gates cannot be met until then |

## Removed Debt

None yet.
