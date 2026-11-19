Decision 001

Frozen SDS is the single source of truth.

---

Decision 002

One Git commit per completed batch.

---

Decision 003

Source discovery uses BaseSource.__subclasses__().

---

Decision 004

Every completed batch must pass:

- pytest
- ruff
- check-config

before commit.

---

Decision 005

Every batch is implemented in a separate Claude conversation.

# Current State

pdate after phase 1 batch 2 

# Architecture Decisions

## D-001

Plugin registration remains explicit through
arip/sources/__init__.py.

Reason

Deterministic imports.

---

## D-002

SourceRegistry discovers plugins using
BaseSource.__subclasses__()

Reason

Required by SDS.

---

## D-003

respx 0.21.1

Known bug:
Issue #277

Temporary workaround:

Always use

url__startswith=

instead of

mock.get(...)

---

## D-004

Source plugins MUST inherit from `BaseSource` directly.
No intermediate / shared base class between source plugins.

Reason

`SourceRegistry` discovers plugins via `BaseSource.__subclasses__()` (D-002),
which returns **direct** subclasses only — it does not recurse.

An intermediate class (e.g. a shared `_HuggingFaceListSource` holding common
fetch/parse scaffolding) would make `__subclasses__()` yield that intermediate
class — which has no `source_id` — instead of the concrete plugins. Every
plugin behind it would silently disappear from all pipeline runs.

Consequence

Structural duplication between sources with similar APIs is accepted and
intentional. This already applies to `arxiv.py` / `huggingface_papers.py` and
now to `huggingface_models.py` / `huggingface_spaces.py`.

Applies to all future source batches.

---

## D-005

Per-source fetch tuning values (result limit, sort key) are module-level
constants inside the plugin, not config fields — unless the SDS explicitly
types that source with its own config subclass.

Reason

SDS §5.15 types `huggingface_models` and `huggingface_spaces` as plain
`SourceConfig`. Adding a `HuggingFaceModelsSourceConfig` subclass to make the
limit tunable would change the frozen config schema.

Only `arxiv` has a dedicated `ArxivSourceConfig` (`max_results`, `categories`)
because the SDS explicitly declares it.

Consequence

Changing a fetch limit for these sources is a code change, not a config change.
Making it configurable requires an SDS amendment.

---

## D-006

Source plugins MAY read their own optional secret directly from `os.environ`.

### Reason

`SourceRegistry` constructs plugins as `cls(source_config)`, passing only that source's own `SourceConfig` block.

Top-level secrets on `AppSettings`, such as `github_token`, are therefore not available through the existing plugin construction contract.

### Rejected Alternatives

- Passing `AppSettings` to plugin constructors — changes the plugin discovery architecture.
- Adding the secret to a per-source config subclass — changes the frozen configuration schema.

### Scope

This decision applies only to optional secrets.

The plugin must function correctly when the environment variable is absent.

This decision is authorized by SDS §5.3.1.


---

## D-007

The project gate in Decision 004 (historical register) is extended: every
completed batch must also pass `ruff format --check .` with zero files that
would be reformatted.

The full gate is: `pytest`, `ruff check .`, `ruff format --check .`,
`python -m arip check-config`.

### Reason

SDS §11 (Phase 0, Objective 1) lists `ruff` as "(linting + formatting)", and
`pyproject.toml` ignores E501 because "line length handled by formatter" —
which was untrue until Batch 10 Stage 1b, because nothing ran the formatter.
Unformatted code accumulated to 27 files (TD-006). Enforcing the check keeps
incidental reformatting out of later diffs, so a stage manifest's hash changes
reflect content changes only.

### Rejected Alternatives

- Lint only (the previous gate) — leaves line length unenforced and lets
  formatting drift back, so a later stage would again mix reformatting with
  logic.
- Running the formatter without gating on it — formatting changes would land
  inside logic diffs.
- `# fmt: off` / `# fmt: skip` regions to keep hand layout — scatters
  exceptions through the code; used only by explicit ruling.

### Scope

Applies to every file ruff lints. `extend-exclude = ["tools"]` removes
`tools/` from both lint and format, unchanged by this decision. Formatting is
defined by the pinned `ruff==0.5.0`; a ruff upgrade that changes formatting is
delivered as its own format-only stage, never inside a logic change. Line
endings follow each file; this decision does not change them.

Introduced in Batch 10, Stage 1b.


---

## D-008

The Phase 4 LLM is `Qwen/Qwen2.5-3B-Instruct`, loaded by `TransformersBackend`
(SDS §5.9) on the GPU with bitsandbytes 4-bit NF4 quantization
(`BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4")`).
`llm.model_name` in `config/settings.yaml` is set to it. Closed for Batch 11.

### Reason

The target GPU is an RTX 4070 Laptop with 8 GB (8188 MiB) VRAM. SDS §7.1 names
"Qwen2.5-3B-Instruct 4-bit (~2 GB)" for 8 GB budgets, and gives multilingual
(Turkish) strength as its reason for preferring Qwen. bitsandbytes is the
quantization method §5.9 specifies for the v1 backend, so no new dependency or
amendment is needed. The quant type is stated because transformers 5.x defaults
to `fp4`.

### Rejected Alternatives

- Qwen2.5-7B-Instruct 4-bit (§7.1 default): written for 12 GB, and leaves little
  headroom on a power-limited laptop GPU.
- Phi-3.5-mini-instruct 4-bit: the §7.1 alternative, but §7.1's multilingual
  reason for Qwen applies.
- Pre-quantized AWQ/GPTQ, or GGUF through llama.cpp: each changes §5.9's v1
  implementation and adds a dependency.
- Unquantized fp16: about 6.2 GB of weights alone on 8 GB.

### Scope

Model and quantization method only. AD-06 is unchanged. Compute dtype and double
quantization follow ruling A15. If the Stage 1 environment check fails, the
TD-019 fallback chain applies, each step by its own ruling. The model card
licence is `qwen-research`; recorded, not ruled. The config/settings.yaml
value is changed in Stage 5, together with the backend that reads it.

Ruling A15 (Batch 11, Stage 5): compute dtype bfloat16 (30.8 against 29.4
tok/s for float16 in the Stage 1 check, a gap under the 10% threshold), no
double quantization (a peak of 2134.8 MiB plus a desktop baseline of about
0.7 GB leaves more than 5 GB of 8188 MiB free), and the non-quantized weights
in bfloat16. Module constants in `arip/backends/llm/transformers_backend.py`,
reported by `get_model_info()`; no configuration fields, so no amendment.

AD-06's rationale cites 12 GB VRAM; the target has 8 GB (8188 MiB). The rule
is unchanged and applies with more force; §9.1 is not altered (ruling Q1).

Introduced in Batch 11.

---

## D-009

`ItemRepository.get_by_status()` returns items ordered by `collected_at`
ascending, then `id` ascending.

### Reason

SQL leaves the order among rows with equal `collected_at` undefined. The
embedding stage processes RANKED items in this order, so among near-duplicates
the order decides which item survives and which is marked `DUPLICATE`. On the
Batch 10 pool, 98 of the 187 embedded rows share their `collected_at` value with
another row (29 groups). SQLite returned such ties in id order in a test, which
is not a guarantee.

### Rejected Alternatives

- Sorting inside `EmbedStage` only: leaves the rank stage, the method's other
  caller, with an undefined order, and moves ordering out of the repository
  that owns the query.
- `embedding_computed_at`, or another timestamp, as the tiebreak: timestamps
  tie on Windows for items processed within one clock tick (Batch 11, Stage 2).
- No change: the survivor of a near-duplicate pair would depend on the query
  planner.

### Scope

SDS §5.14 lists the method's signature and no ordering, so this needs no
amendment. The rank stage's results do not depend on the order: its cohort
percentiles are computed over the whole batch. `get_by_status_and_language()`
is not affected; its order is TD-023 (a), delivered in Stage 4. Pinned by
`test_get_by_status_orders_equal_collection_times_by_id`, which checks the
generated SQL, because SQLite's row order alone cannot tell the two queries
apart.

Introduced in Batch 11, Stage 3.

---

## D-010

`ItemRepository.get_by_status_and_language()` orders items by `importance_score`,
then the `engagement` and `topic` values in `signal_breakdown`, then
`published_date`, each descending with a missing value last, then by `id`
ascending. The sort runs in Python after the query.

### Reason

TD-023 (a): the order among equal scores was undefined, and the generation
stage takes its capped selection from this order (SDS §9.2.1). On the Batch 10
pool, 60 of the 179 ENRICHED items tie on score in 9 groups; engagement
separates 10 of them, topic and published_date none, and id the remaining 50,
including a 16-row tie at the second cutoff (Batch 11, Stage 2).

### Rejected Alternatives

- Score only: the order within ties stays undefined.
- `json_extract()` in SQL: SQLite-only, and one malformed `signal_breakdown`
  fails the whole query.
- `published_date` before engagement: it separates nothing on the measured
  pool, and recency is already part of the score.

### Scope

SDS §5.14 states no ordering, so no amendment is needed. A missing,
unparsable, non-object or non-numeric breakdown value counts as missing and
sorts last on that key. The method has no runtime caller before Stage 8.
Pinned by the `test_selection_order_*` tests in `tests/unit/test_repositories.py`.

Introduced in Batch 11, Stage 4.

---

## D-011

`TransformersBackend.generate()` (SDS §5.9) receives the Jinja2 output and
wraps it as exactly one user message with the tokenizer's chat template
(`add_generation_prompt=True`), keeping the tokenizer's default system message
unmodified. `generated_content.full_prompt` stores the Jinja2 output, not the
wrapped string. Each call uses a fully specified `GenerationConfig` built only
from `GenerationParams`, with top-k disabled (`top_k=0`); `temperature <= 0`
means greedy decoding. `stop_reason` is decided in the order eos,
stop_sequence, max_tokens, and a matched stop string stays in `text`. A set
seed is applied with `transformers.set_seed()` before the call.

### Reason

- A8: the templates stay model-independent, and `StubLLM` tests see no chat
  markers. §4.3 and §5.8 call `full_prompt` the "rendered prompt"; this reads
  that as the template output. Qwen2.5-Instruct's chat template inserts "You
  are Qwen, created by Alibaba Cloud. You are a helpful assistant." when no
  system message is given; an explicit system text is prompt design (Stage 6).
- Q3: transformers 5.20.2 fills every generation value left `None` from the
  checkpoint's `generation_config.json` (Qwen ships sampling defaults such as
  `top_k` 20). Those values would otherwise apply silently, and a greedy call
  would sample. The backend therefore replaces the model's generation config
  at load with one holding only its token ids, logs the dropped values
  (`dropped_generation_defaults` in `llm_loaded`), and sets every sampling
  value on each call. This also drops the model's own `repetition_penalty` in
  favour of `GenerationParams.repetition_penalty` (1.1). If Stage 9 output
  shows repetition, that is a calibration question for later; no action now.
- Q4: `temperature <= 0` sets `do_sample=False` and the neutral temperature and
  top_p of 1.0. transformers then logs once that `top_k` is ignored in greedy
  mode; top-k has no effect there.
- Q5: eos first, as the model's own end. A stop string counts when it appears
  anywhere in the decoded text: transformers stops at the token that completes
  it, and that token may run past the string's end. Keeping the string keeps
  `raw_output` literal; the Stage 7 parser handles it. A generation that ends
  with none of the three raises `LLMError`.
- Q6: `set_seed()`, as verified in Stage 1. It also seeds Python's `random`
  and numpy globally, as well as torch on every device.

### Rejected Alternatives

- Chat markers in the `.j2` templates (A8 option ii): ties every template to
  one model family.
- Storing the wrapped string (A8 option iii): needs a new field or method,
  that is, a §5.9 amendment.
- Letting the checkpoint's defaults fill the gaps (Q3 option a).
- Rejecting `temperature <= 0`, or passing it through (Q4 options b and c).
- Seeding torch only (Q6 option b).

### Scope

Backend only. The template text, any system message, and range validation of
the generation settings belong to Stage 6 (§5.15 amendment); what the caller
does with an `LLMError` is ruling A18 (Stage 7). §5.9 states none of these, so
no amendment is needed. Pinned by `tests/unit/backends/test_llm.py`.

Introduced in Batch 11, Stage 5.

---

## D-012

The real LLM backend is integration-tested against the production model named
in `config/settings.yaml` (`Qwen/Qwen2.5-3B-Instruct`, D-008), loaded from the
local Hugging Face cache, by `tests/integration/test_llm_gpu.py`. The test is
marked `gpu` and runs only with `--run-gpu` (`tests/conftest.py`). It skips
unless torch, transformers and bitsandbytes import, CUDA is available,
`HF_HUB_OFFLINE=1` is set before the run, and the model is cached.

### Reason

SDS §5.9: "The real backend is integration-tested separately against a small
model (~1B parameters)." The 3B model was downloaded in Stage 1, so the test
needs no new download and exercises the exact shipped configuration. The
default gate stays GPU-free, as the Phase 4 gate requires ("`StubLLM` tests
pass independently of any GPU"); because the test is opt-in, a CUDA venv does
not load the model on every test run either.

### Rejected Alternatives

- Qwen2.5-0.5B- or 1.5B-Instruct: closer to "~1B", but a new download and not
  the shipped model.
- A tiny random Qwen2 model built by the test: no download, but not verified
  on Windows with CUDA.
- A `-m "not gpu"` default in `addopts`, or an environment variable (Q8
  options b and c); skip conditions alone (option d) would run the test in the
  default gate in arip-llm.

### Scope

Departs from the letter of §5.9 ("~1B"). The test asserts a 4-bit NF4 load,
non-empty output, `stop_reason == "max_tokens"` at `max_new_tokens=8`,
`is_loaded` false after exit, and `torch.cuda.memory_allocated()` after exit at
most 16 MiB above the value before loading (Stage 1 measured an 8.1 MiB
residue, the cuBLAS workspace; reserved memory is not required to be zero).
Outside the default gate, so no mutations. The default suite gains one skipped
test in every environment.

Introduced in Batch 11, Stage 5.