# Batch 9 — Decisions Record

**Scope:** Embeddings and semantic deduplication (SDS §5.6, §5.7, plus the
`EmbedStage` that drives them).

**Status at time of writing:** Stages 1–3 applied. Stage 4 outstanding.

**What this file is.** A working record of every ruling made during Batch 9, so
that Stage 4 and any later reader can act on them without reconstructing the
conversation they were made in. It is not the completion report and does not
replace it; it records *what was decided and why*, not what was built.

**What this file is not.** It is not an amendment to the SDS and has no
authority over it. Where a decision corrects or resolves the SDS, that is stated
explicitly and the SDS text is quoted. `docs/frozen_sds.md` remains the single
source of truth.

---

## A1 — `BaseEmbedder.embed()` returns `np.ndarray`

**SDS:** §5.6; `arip/interfaces.py` (Phase 0).

`BaseEmbedder.embed()` was typed `-> list[list[float]]`. It now returns
`np.ndarray`, float32, shape `(len(texts), embedding_dim)`.

**Why the ABC was what changed.** §5.7 hands vectors to `usearch`, which is a
NumPy-native library; §5.6 describes "unit vectors of the correct dimension".
Returning lists of lists would mean converting to an array at every call site
and converting back nowhere — the list type would exist only in the annotation.
The alternative, converting inside `SemanticDeduplicator`, would have pushed the
same cost one layer down and left the ABC describing a shape nothing produces.

**Why this was safe here and will not be again.** Nothing implemented
`BaseEmbedder` at the time of the change — verified: `BaseEmbedder.__subclasses__()`
returned `[]` and no module in `arip/` referenced it outside `interfaces.py`.
This is the first and, for this interface, the only moment at which a Phase 0
signature could change without breaking a caller.

**Consequence for the optional extra.** `numpy` is imported in `interfaces.py`
under `TYPE_CHECKING` only. `interfaces.py` is imported by effectively the whole
application; a runtime import there would make the optional `embedding` extra
mandatory and contradict `pyproject.toml`.

---

## A2 — Check first, add after

**SDS:** §3.3 (`RANKED → EMBEDDED`) versus §5.7 (Semantic Deduplication).
**Conflict, resolved in favour of §5.7.**

§3.3 lists the action for `RANKED → EMBEDDED` as "Set `embedding_computed_at`,
`embedding_model_name`; **add vector to ANN index**". §5.7 says "On each run,
embeddings for new items are added **after the dedup check**."

**Ruling: §5.7 governs. The order is `is_duplicate()` first, `add()` second.**

**The consequence of the other reading, stated plainly.** Adding the vector at
`RANKED → EMBEDDED` puts the item in the index *before* it is checked. Its
nearest neighbour is then itself, at cosine similarity 1.0, which is above any
threshold. Every item would be marked `DUPLICATE` of itself — and `DUPLICATE` is
terminal (§3.3: "No automatic re-processing"), so the pipeline would silently
destroy 100% of its output on the first run that reached this stage.

**Corroboration inside the SDS.** §5.7's reconciliation query excludes
`DUPLICATE`, `FILTERED` and `FAILED`. If items were indexed before the check,
every item would end up `DUPLICATE` and the reconciliation query would return
nothing — the query only makes sense under the §5.7 ordering.

---

## A3 — The four methods of §5.7, and no `check_and_add()`

**SDS:** §5.7 class sketch.

`SemanticDeduplicator` exposes `load_index()`, `save_index()`,
`is_duplicate(vector, item_id)` and `add(item_id, vector)` with the signatures
§5.7 gives, and does **not** offer a combined `check_and_add()`.

**Why.** A fused method would be more convenient and would hide the exact
ordering A2 turns on. Two calls at the call site make the order reviewable in
`EmbedStage`; one call makes it invisible and unenforceable.

**One addition, and its justification.** `contains(item_id) -> bool` is public.
§5.7's own reconciliation text requires it in as many words — "check if
`item_id` is in the loaded index. If not, re-embed and add." It is a read-only
query, not a fifth behaviour, and is not what A3 rules out. `size` and
`threshold` are read-only properties for diagnostics and tests.

---

## A4 — `EMBEDDED → ENRICHED` sets `enriched_at` only

**SDS:** §3.3 (`EMBEDDED → ENRICHED`), §4 items table.

§3.3 gives the action as "Set metadata fields, `enriched_at`". The "metadata
fields" belong to enrichment, which this batch does not implement. In Batch 9
this transition sets `enriched_at` and nothing else.

**Why this is not a stub.** The transition itself is real and its timestamp is
real; what is absent is enrichment content, which no SDS section assigns to
Phase 3. This is a complete implementation of the part of the transition that
Phase 3 owns, not a placeholder for it.

---

## A5 — `EmbedStage` owns all three transitions

**SDS:** §3.3, AD-03, §5.14.

The three transitions reachable from this stage —

| From | To | Trigger |
|---|---|---|
| `RANKED` | `EMBEDDED` | `embed_ok` |
| `RANKED` | `FAILED` | `embed_error` |
| `EMBEDDED` | `ENRICHED` / `DUPLICATE` | `dedup_pass` / `dedup_hit` |

— are all driven by `EmbedStage`, through `StateMachine.transition()`, which
AD-03 makes the sole mutator of `status`.

**Why one stage and not two.** Splitting embedding and deduplication into
separate stages would put `EMBEDDED` items in the database between two session
scopes, and a crash in the gap would strand them: `EMBEDDED` has no retry path
in §3.3. Keeping both in one stage means an item reaches a durable resting state
(`ENRICHED`, `DUPLICATE` or `FAILED`) within the pass that created it.

---

## A6 — Reconciliation at stage start, once per run

**SDS:** §5.7 "Index Reconciliation on Restart".

§5.7 says reconciliation "runs once at startup". It runs at the start of
`EmbedStage`, once per run, not in `container.py`.

**Why not literally at startup.** Reconciliation re-embeds, and re-embedding
needs a loaded model. AD-06 forbids the embedder and the LLM being resident
simultaneously, and §5.6 makes the embedder a context manager precisely so it is
loaded only for the duration of the embedding pass. Reconciling in the container
would either load a model at process start and hold it for the whole run, or
reconcile without the means to re-embed. The start of the embedding pass is the
first moment both conditions are satisfiable, and for a process that performs one
run it is the same moment.

**Data note.** `items` stores `embedding_computed_at` and
`embedding_model_name` but not the vector (§0 change log: "The ANN index file is
the sole vector store"). Reconciliation therefore *re-computes* vectors for
items missing from the index; it cannot copy them from the database.

---

## A7 — `usearch` is not pure Python; the choice stands

**SDS:** §5.7, §11.

§5.7 justifies `usearch` as "(pure Python, no native BLAS dependency issues on
Windows)". **The parenthetical is factually wrong.** Measured on usearch 2.26.2:
the wheel ships compiled extension modules (`compiled.cpython-311-*.so` and a
bundled `numkong` native module).

**Ruling: keep `usearch`; do not amend the SDS; record the correction here.**
The *conclusion* the SDS drew is still correct — it installs from a prebuilt
wheel on Windows and needs no BLAS, which is what the sentence was defending.
Only the stated reason is wrong, and no code depends on the reason. The
correction is repeated in `arip/dedup/semantic.py`'s module docstring so it is
visible to anyone reading the code that acts on it.

---

## A8 — `top_k` is the search `k`; only the closest hit is used

**SDS:** §5.7; `config.dedup.top_k` (default 5).

`top_k` is passed as `k` to `index.search()`. Of the returned hits, only the
closest one that survives self-exclusion is used for the threshold decision.

**Why `top_k > 1` is nonetheless load-bearing.** It is not there so that several
candidates are considered — the SDS defines the decision against "the nearest
neighbor" (§3.3, `dedup_hit`). It is there so that when the closest hit is the
item itself (A2 / self-exclusion), a genuine neighbour is still available behind
it. With `top_k = 1` a self-match would consume the only slot and the item would
be reported unique. `test_self_exclusion_still_finds_a_real_duplicate` pins this.

---

## A9 — Novelty is written at `RANKED → EMBEDDED`, merged not overwritten

**SDS:** §5.5 note ("Novelty signal"), AD-19, §4 (`signal_breakdown`).

The SDS: "Novelty is computed as a byproduct of semantic deduplication — the ANN
distance to nearest neighbor is stored in `items.signal_breakdown` but **is not
used in the ranking score**." AD-19 says the same at architecture level.

**Two consequences.**

1. **Novelty never enters `Scorer`.** `importance_score` is the four-signal sum
   Batch 8 implemented. Adding novelty to it would contradict AD-19 and change
   every score already in the database.

2. **`signal_breakdown` must be read, merged and written back — never
   overwritten.** `RankStage` (Batch 8) writes the four signals into this column
   at `COLLECTED → RANKED`. Writing a fresh `{"novelty": ...}` object at
   `RANKED → EMBEDDED` would destroy them. The write is: load the existing JSON,
   set the novelty key, serialise the whole object.

**Where the value comes from.** The ANN distance to the nearest neighbour, which
`is_duplicate()` already computes. Note that items with no neighbour (the first
items of the first run) have no defined novelty; Stage 4 must decide what is
written in that case, and that decision is not yet made.

---

## Write order — database commit before `save_index()`

**SDS:** §5.7, §5.14; established as a Batch 9 constraint.

**Ruling: commit the database transaction first, then call `save_index()`, once
per run.**

**Why the reverse is unsafe.** The two stores can disagree in exactly two ways,
and they are not symmetric:

| Disagreement | Recoverable? |
|---|---|
| Item in the **database**, missing from the **index** | **Yes.** This is precisely what §5.7's reconciliation repairs — it walks the query result and adds anything the index lacks. |
| Item in the **index**, missing from the **database** | **No.** Reconciliation runs *from* the database *to* the index. It has no way to discover an index key that no row refers to, and no way to decide whether it is stale or authoritative. The orphan stays forever, matching future items against an item that does not exist. |

Committing first means a crash in the gap produces only the recoverable
direction. Saving first means a crash produces the unrecoverable one.

**Once per run, after the commit.** Not per item: `save_index()` serialises the
whole index, so per-item saving is O(n²) writes across a run for no additional
safety — a crash mid-run is repaired by reconciliation either way.

---

## Self-exclusion in `is_duplicate()` — crash recovery, not defensive coding

**SDS:** §5.7; established as a Batch 9 constraint.

`is_duplicate(vector, item_id)` ignores any hit whose key equals `item_id`.

**Why it exists even though A2 makes it unreachable in a clean run.** Under A2
an item is never in the index when it is checked, so in a run that completes
normally this guard never fires. It exists for the run *after* a crash between
the database commit and `save_index()` — or after any interruption that leaves
the index holding an item whose processing did not finish. Reconciliation re-adds
that item at the start of the next run; without self-exclusion the item then
matches itself at similarity 1.0, is marked `DUPLICATE`, and — since `DUPLICATE`
is terminal — is destroyed by the recovery mechanism that was meant to repair it.

**This is why it is not defensive coding.** It guards a state the system's own
recovery path produces, not a state that "should never happen".

---

## Stub similarity is bimodal — near-threshold tests use constructed vectors

**SDS:** §5.6 testing strategy, §5.7 testing strategy.

**Measured across 384 dimensions:**

| Pair | Cosine similarity |
|---|---|
| Identical text | 1.0000 |
| One trailing period added | 0.0024 |
| One character truncated | 0.0382 |
| Unrelated text | −0.0457 |

The stub yields 1.0 or approximately 0, with nothing between. This is not a
defect: 384-dimensional Gaussian unit vectors are near-orthogonal by
construction, and the stub is deterministic precisely so that identical text
gives 1.0 (see the stub decisions below).

**Consequence: the 0.92 threshold cannot be exercised by varying text.** Tests
that manufacture "similar-looking" strings and assert on whatever similarity
emerges are testing nothing — every such pair lands near 0.

**Ruling: build near-threshold pairs as vectors and pass them directly to
`is_duplicate()`.** §5.7's testing strategy already describes this — "known
similar pairs using mock embeddings (**cosine similarity calculated
directly**)". `tests/unit/dedup/test_semantic.py` constructs unit vectors at an
exact chosen angle, and asserts either side of the threshold at three decimal
places. The stub's role is confined to paths where the pipeline produces the
vector; every threshold assertion uses a constructed one.

---

## Stub backend decisions

**SDS:** §5.6 testing strategy; D-004, D-005; §5.15.

| Decision | Value | Source |
|---|---|---|
| `backend_id` | `"stub"` | **Chosen.** §5.6 names the file and the concept, never an id string. Taken from the filename stem, which yields `sentence_transformers` for the real backend by the same rule. |
| Selection | `EmbeddingRegistry` matches `config.embeddings.backend` against `BaseEmbedder.__subclasses__()` | **SDS-determined** (§1.2, AD-02). One resolved backend rather than a dict of all of them follows §5.9's `llm_registry.get_backend()`. |
| `embedding_dim` | `384`, module constant `STUB_EMBEDDING_DIM` | **Value SDS-determined** (§5.6 names 384 and requires "the correct dimension"). **Module constant rather than config is chosen**, per D-005 — `EmbeddingSettings` is typed with `backend` and `model_name` only. |
| Unknown backend id | `ConfigError` at construction, listing the available ids | **Chosen**, derived from §5.15 ("Invalid config → `ConfigError` → process exits with a clear message"). Raised in `__init__` so the failure lands at startup while the message can still enumerate alternatives. |
| Deterministic, not random | SHA-256(text) → seeded generator → normalised Gaussian | **Chosen.** §5.6 says "random unit vectors". Literally random vectors make §5.7's "known similar pairs" untestable, and `hash()` is salted per process so vectors would not survive a restart. The distribution is unchanged; only reproducibility is added. |

### Stub model-name marking — defect found and fixed during Stage 2

`StubEmbedder.model_name` returns `f"stub:{configured_model_name}"`, always
prefixed. `STUB_MODEL_NAME_PREFIX` is exported for anything that needs to match
on it.

**The defect.** The stub originally defaulted `model_name` to `"stub"` and
relied on that default to mark itself. But `EmbeddingRegistry.get_backend()`
passes `config.model_name` to whichever backend it selects, so the default never
applied: a run with `embeddings.backend: "stub"` recorded
`embedding_model_name = "all-MiniLM-L6-v2"`.

**Why that was serious rather than cosmetic.** §5.7's reconciliation selects on
`embedding_computed_at IS NOT NULL` and cannot distinguish a stub row from a
real one. The ANN index would fill with stub vectors labelled as real, and
switching to the real backend would leave the data poisoned with nothing in the
row to detect it. §5.6's stub is not test-only — it is also how an operator
exercises the pipeline without a model download.

**The `__init__` default stays `"stub"`, deliberately.** A caller who named no
model stood in for nothing; defaulting to a real model name would reintroduce
the same shape of untruth at smaller scale. A bare `StubEmbedder()` therefore
reports `"stub:stub"` — degenerate and accurate. Production never reaches it,
because the registry always supplies `config.model_name`.

---

## `SemanticDeduplicator` construction and failure modes

Recorded here because Stage 4 depends on them.

- **`embedding_dim` is a constructor argument.** §5.7 says `load_index()`
  "loads from disk **or creates new**", and an index cannot be created without a
  width. **Open for Stage 4:** where `container.py` obtains that width. The real
  backend only knows its dimension after `__enter__`, which conflicts with AD-11
  constructor injection. Not yet decided.

- **Index of the wrong width is treated as corrupt.** Measured: usearch silently
  adopts the width recorded in the file, overwriting the `ndim` the `Index` was
  constructed with. Left alone, every later search would raise `ValueError`,
  §5.7's malformed-vector rule would swallow each one individually, and the run
  would find no duplicates while reporting nothing wrong. **Chosen:** delete and
  rebuild, with a warning — turning a silent permanent fault into one loud
  expensive run.

- **`save_index()` is atomic.** Written to a sibling temp file and moved with
  `os.replace`. **Chosen**, not in the SDS: without it a crash mid-write leaves a
  truncated file that `load_index()` can only treat as corrupt, costing a full
  re-embed. With it, an interrupted save leaves the previous index intact and the
  run's additions are recovered by reconciliation.

- **`add()` tolerates a key already present.** Measured: usearch raises
  `RuntimeError("Duplicate keys not allowed in high-level wrappers")`.
  Reconciliation legitimately revisits indexed items, so this is logged and
  skipped rather than raised.

- **Malformed vector.** §5.7: "log, skip semantic check, continue with exact
  check only." `is_duplicate()` returns `(False, None)`; `add()` skips. Measured
  caveat: usearch raises `ValueError` on a **wrong-dimension** vector but accepts
  a **NaN** vector silently, so §5.7's "usearch raises on malformed vector" is
  only partly true. NaN detection is not implemented in this batch and is not
  covered by any SDS requirement.

- **The threshold comparison is `>=`, and float32 makes that hard to pin.**
  §5.7 and §3.3 both specify "≥". A constructed vector cannot test the boundary
  exactly: `vector_at_similarity(0.92)` measures 0.9200000017881393 through the
  index — strictly above 0.92 — so such a test passes under both `>=` and `>`
  and pins nothing. The one exact construction is a vector searched against
  itself, where the distance is exactly 0.0 (measured, for both axis-aligned and
  arbitrary directions). `test_threshold_is_inclusive_at_exact_equality` uses a
  threshold of 1.0 with a differing `item_id`, and is the only test in the suite
  that fails when `>=` becomes `>`.

- **`is_duplicate()` returns the nearest neighbour even below threshold.**
  **Chosen.** §5.7 names the field `nearest_neighbor_id`, not `duplicate_of_id`,
  and a near miss is worth having in the log. `None` means the index holds no
  other item, or the search was skipped.

---

## Dependency ruling and TD-019

**SDS:** §11 (Dependencies).

**Ruling: `sentence-transformers==6.0.1`, `usearch==2.26.2`, as a
`[project.optional-dependencies] embedding` extra.**

**Why not the previously recorded pins.** The project had recorded
`sentence-transformers==3.0.1`. The only argument for it was compatibility with
the recorded `transformers==4.43.0`. That argument was **measured and
disproved**: installed today, `sentence-transformers==3.0.1` resolves against
`transformers 4.57` and `torch 2.14` — a combination it was never tested with.
§11 pins no versions itself; it names packages and states the purpose of pinning
("It also documents exactly what version combination was tested"). Since the
recorded combination is not the combination that installs, choosing current
versions together serves that purpose better than preserving a pin whose
rationale no longer holds.

**Why an extra rather than a core dependency.** The source and ranking layers
must install without torch. `pyproject.toml` says so, and the code enforces it:
`numpy`, `sentence_transformers` and `usearch` are imported under
`TYPE_CHECKING` or inside functions, never at module level, throughout
`arip/backends/embeddings/` and `arip/dedup/`.

**Platform note.** An earlier measurement of ~5.9 GB for the torch stack was
taken on Linux and does not apply to the project's target platform: every
`nvidia-*` and `triton` requirement of torch is gated by
`platform_system == "Linux" and platform_machine == "x86_64"`, so on Windows
none install. Measured on Windows 11 / Python 3.11.9: ~215 MB of wheels,
`torch 2.14.0+cpu`, no NVIDIA packages. The 5.9 GB figure is Linux-only.

**TD-019** covers the commented-out `llm` line in `pyproject.toml`, whose every
pin is a 2024 version. The `embedding` extra puts the environment on
`transformers` 5.x while that line pins `transformers==4.43.0`; the two cannot
coexist. The whole line must be re-pinned before Phase 4 uncomments it, and
§5.8 / §5.9 were written against `transformers` 4.x.

---

## Test-count note for the batch entry

The `embedding` extra changes the suite's shape, and the two figures collide
with an earlier baseline in a way that could mislead a reader:

> **739 passed (embedding extra ile), 661 passed, 2 skipped without it, the
> single skip standing for the whole module.**

(Figures as of Stage 2. Stage 3 adds `tests/unit/dedup/`, making it **702
passed** with the extra and **624 passed, 2 skipped** without.)

`624 passed` is exactly the Batch 8 baseline. A reader who sees a green suite on
a `[dev]`-only install has no signal that the embedding tests never ran — a
module-level `pytest.importorskip` is reported as **one** skip, not one per test,
and its reason line appears only under `-rs`. Both configurations must therefore
be stated together wherever the batch's test count is recorded.
