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

---

## D-013

Prompt templates (SDS §5.8, §5.9.1). One template per use case and language in
`config/prompts/`: `thread_en.j2` is the only one in Phase 4 and serves every
source type. A file is a YAML header, a line holding only `---`, and the Jinja2
body. The header keys are exactly `template_id`, `language`, `description`,
`required_variables`, `optional_variables` and `system`; an unknown or missing
key, a filename other than `{template_id}_{language code}.j2`, a blank `system`,
and any difference between the variables the body uses and those the header
declares are `ConfigError` at load. Rendering uses a sandboxed environment with
`StrictUndefined`. Every missing value (`None`, a blank string, an empty
collection) is dropped from the context first, so an optional field takes its
`default(...)` or `is defined` branch, and a missing required variable raises
`jinja2.UndefinedError` naming it. The header's `system` text is sent as the
system message (§5.9.1), which supersedes D-011's sub-ruling A8 (keep the
tokenizer's default) for every templated generation. The stored
`prompt_template_id` is `thread_en`; the stored `full_prompt` is
`"### SYSTEM\n" + system text + "\n\n### USER\n" + rendered body`, built by
`stored_prompt()`.

### Reason

- A9: a fixed header makes "required variables" explicit (§5.8 says "validate
  that all required variables are provided" without saying how they are
  declared); the declared-against-used check makes a misspelt variable fail at
  load instead of silently taking its default. One template: "type" in §5.10 is
  read as the use case, and the Phase 4 deliverable is "English template".
- Missing values (ruling Q6): Jinja2's `default` filter does not fire on
  `None` (verified with Jinja2 3.1.4), so §5.8's "The renderer never injects
  Python `None` as a string" needs the renderer to drop such keys. Blank strings
  and empty collections are dropped too, matching §9.2.1's `str.strip()`.
- A10 (ii): required `title`, `primary_url`, `source_type`, `source_id`;
  optional `abstract`, `authors` (first five, then ", and others"),
  `published_date`, `source_signals`. No ranking scores. These fields are
  supplementary input only; eligibility stays on `abstract` (§9.2.1). A12 (ii):
  `primary_url` is required, so an item without one fails before any LLM call.
- System text (rulings Q1–Q3): a role statement for ARIP instead of "You are
  Qwen, created by Alibaba Cloud…", kept in the template so that the wording is
  prompt design under git and each language carries its own.
- `full_prompt` (ruling Q2): §4.3 calls it the "Complete rendered prompt sent to
  LLM"; the layout keeps both messages and can be split again. The chat markers
  added by the tokenizer are not stored (D-011).

### Rejected Alternatives

- Free-form metadata with required variables inferred from the body; one
  template per source type; lenient undefined (A9 options).
- Dropping only `None` (Q6 option a).
- The system text as a `GenerationParams` field, a backend constant, or not at
  all (Q1 options b–d); storing the rendered body only (Q2 option i).

### Scope

`config/prompts/thread_en.j2` is approved verbatim (2026-11-21, SHA-256
4e721acf…94fa with LF line endings). The context builder that fills these
variables from an item is Stage 7; the startup lookup of the template for each
configured language is Stage 8 (ruling Q10). Pinned by
`tests/unit/generation/test_prompt_manager.py`, including a golden render of
the shipped template.

Introduced in Batch 11, Stage 6.

---

## D-014

Validator conventions (SDS §5.11, §5.11.1). `parsed_segments` is a JSON list of
`{"role": <SegmentRole value>, "text": <str>}` objects in canonical order (HOOK,
PROBLEM, SOLUTION, IMPACT, TAKEAWAY, SOURCE), and `full_text` is the segment
texts joined by a blank line, without markers; the parser that writes them is
Stage 7. `ContentValidator.validate()` runs every check and collects every
error. A `parsed_segments` of `None`, or one not in this format, is a parse
error, after which only the truncation check runs. The citation check needs the
exact `primary_url` string inside a SOURCE text, with no normalisation, followed
by a URL boundary: the end of the text, whitespace, one of `)` `]` `}` `>` `"`
`'`, or a `.` or `,` that is itself followed by the end of the text or
whitespace. Any other following character continues the URL and fails (a
letter, a digit, `/`, `-`, `_`, `~`, `?`, `#`, `&`, `=`, `%`, `+`, a `.`
followed by a non-space, and every character not listed as a boundary). Every
occurrence of the URL is tried. Each
segment needs at least 30 characters after `str.strip()`, SOURCE included.
Prohibited phrases match as case-insensitive (casefolded) substrings of
`full_text`. The numeric check uses §5.11's two example patterns (percentages
and "N x"); a number is supported when it occurs in the abstract or in a
`source_signals` value and is not part of a longer number. Warnings never fail a
row and are logged at WARNING level as `validation_warning`.

### Reason

- Q11: the validator reads the stored format, so it is fixed in the same stage
  as the validator rather than left to the parser.
- Q12: every error at once gives a specific `failure_reason` and shows the model's
  failure pattern in Stage 9.
- A12: the URL is given to the model verbatim, so an exact match is the test of
  "cited". A13: simple and predictable; the list is empty by default (§5.15.2).
- A12 boundary (ruled 2026-11-21, Stage 6b): a plain substring test passed a
  SOURCE holding a longer URL that begins with `primary_url` (for example
  `…/abs/1234` inside `…/abs/12345`). The boundary is a reading of §5.11's
  "appears in the SOURCE segment", so no SDS amendment.
- A14 (i): HF models and spaces carry no abstract, and wider patterns would
  raise the warning rate before there is data; the boundary keeps "5%" from
  being covered by "15%" and "12" by "12.5". The title is not a source (§5.11
  names `source_signals` and `abstract`).
- Q5 (a): the template asks for a sentence ending with the link, so SOURCE can
  meet the 30-character minimum (a bare `https://github.com/a/b` has 22).

### Rejected Alternatives

- Stopping at the first error (Q12). Case-sensitive or word-boundary phrase
  matching (A13 ii, iii). Wider numeric patterns (A14 ii, iii). URL
  normalisation. Exempting SOURCE from the minimum (Q5 b).
- A plain substring test for the citation (Stage 6 as first delivered).

### Scope

Stage 6 delivers the validator and `stored_messages()`; the generator (Stage 7)
calls them, stores the row and decides retries (§5.10). Pinned by
`tests/unit/generation/test_validator.py`.

Introduced in Batch 11, Stage 6.
Citation boundary added in Batch 11, Stage 6b.

---

## D-015

Generator conventions (SDS §5.10, §5.10.1). `ContentGenerator.generate(item)`
uses the `thread` template in the item's language (a `ConfigError` propagates).
A missing required variable makes it return `None` with `last_failure_reason`
"Template error: …", without an LLM call or a row. Otherwise it makes at most
`max_retries + 1` attempts, numbered after the item's highest stored attempt
(`ContentRepository.max_attempt_for_item()`). Attempt seeds are
`(base + attempt − 1) mod 2³²`; the base is the configured seed, or, when that is
null, one value per call from `secrets.randbelow(2**32)`. Only the seed differs
between attempts. Each attempt sends the rendered body with the template's system
text, parses the output (D-016), validates a transient row with the attempt's
`stop_reason`, and stores the row with `validation_errors` =
`json.dumps(stored_messages(result), ensure_ascii=False)` (always a JSON list,
`"[]"` when empty). A failed attempt is stored with `is_active=False`; a passing
one first calls `deactivate_all_for_item()` and is stored with `is_active=True`.
Invariants: at most one active row per item, and an active row has passed
validation. An `LLMError` is not retried: `last_failure_reason` becomes "LLM
error: …" and the error propagates. When every attempt fails, `generate()`
returns `None` with "Validation failed after N attempts: " and the last
attempt's errors. Every failure is recorded by the caller with
`failed_at_stage='GENERATION'`. The generator never commits, and it logs one
`generation_attempt` event per attempt (token counts live only there).

### Reason

- A6 (i): an active row is one "selected for review/publishing" (§4.3), so a
  failed attempt cannot be active. Continued numbering keeps
  `get_by_item_and_attempt()` unambiguous on a second pass and keeps seeds from
  repeating.
- A7 and the overflow rule: one formula for both cases; modulo keeps every seed
  inside the range numpy accepts (§5.15.2). `secrets`, not `random`:
  `transformers.set_seed()` reseeds Python's `random` on every call, so a base
  drawn from `random` would follow from the previous item's seed.
- A18 (i), A19 (ii): §5.10.1. Q2: §3.3's ENRICHED → FAILED transition sets
  `failed_at_stage='GENERATION'`; the `VALIDATION` value appears only in §3.3's
  re-queue table, and no transition sets it. The reason prefixes ("Validation
  failed after …", "Template error: …", "LLM error: …") tell the cases apart.
- Q8: a transient row is validated before the insert, so every column is
  written once; validator warning logs then carry `content_id=None`, and
  `item_id` still identifies the row. Q9: readers can always `json.loads()`.
- Q14: the last attempt's errors are the most recent evidence; every attempt's
  errors stay in its own row.

### Rejected Alternatives

- Failed attempts active, or all rows active (A6 ii, iii). Restarting attempt
  numbers at 1 (Q7). A null seed staying null (A7 i), or a base from the item id
  (A7 iii). Clamping or raising at 2³² − 1 (Q6). Retrying an `LLMError` (A18 ii).
  Storing a placeholder row for a template error (A19 iii). Passing `AppSettings`
  to the generator (Q4); a result object instead of the attribute (Q5).

### Requirements on Stage 8 (reviewer, 2026-11-23)

1. §5.10.1 says that rows stored for earlier attempts are kept when an `LLMError`
   propagates. The generator never commits, so this depends on the caller: after
   an `LLMError`, the stage marks the item FAILED and commits, and it must not
   roll back the session in a way that discards those rows. The Stage 8 PRE-CODE
   shows how this is met and pins it with a test.
2. Open for the Stage 8 PRE-CODE, no ruling now: when a regeneration pass (an
   item re-queued from PENDING_REVIEW) fails every attempt, the previous active
   row stays active while the item becomes FAILED, because deactivation runs
   only on success. Stage 8 rules whether that is acceptable, or whether a pass
   deactivates earlier rows at its start.

### Scope

`arip/generation/generator.py` and `arip/db/repositories/content.py`; the item
transitions, `generated_at` and `failure_reason` are written by the generation
stage (Stage 8). Pinned by `tests/unit/generation/test_generator.py` and
`tests/unit/test_content_repository.py`.

Introduced in Batch 11, Stage 7.

---

## D-016

Context builder and output parser (SDS §5.10, rulings A10 and A11).
`build_context(item)` returns exactly the variables of `thread_en.j2`: `title`,
`primary_url`, `source_type`, `source_id` unchanged; `abstract` stripped;
`authors` from the JSON list, entries as stripped strings with blanks dropped;
`published_date` in ISO form; `source_signals` from the JSON object unchanged.
JSON that does not parse, or has the wrong shape, counts as missing; the renderer
drops missing values (D-013). `parse_output(raw)` recognises a marker line
`[HOOK]` … `[SOURCE]` at the start of a line, in any case, with up to two `*` or
`_` around it, spaces inside the brackets and an optional `:` after it. The
segment text is the rest of the marker line plus the following lines up to the
next marker, stripped at both ends. Text before the first marker is dropped and
its length logged; a marker inside a line is text; text after SOURCE belongs to
SOURCE; roles out of order are stored in canonical order; a missing role parses;
a marker without text gives an empty segment. No marker at all, or a repeated
role, is a parse failure (`None`). Storage as D-014, with
`ensure_ascii=False`.

### Reason

- A10 (ii): the same mapping as the Stage 6 measurement, so the measured prompt
  sizes apply; no ranking field reaches the model.
- A11 (ii): a 3B model decorates markers; rejecting `**[Hook]**` costs a full
  generation without protecting quality. A repeated role is ambiguous; a missing
  role is the validator's completeness error, with a specific message.
- Q11: separate `context.py` and `parser.py` beside the three files §6 lists, so
  each is tested on its own. Q12: rows stay readable.

### Rejected Alternatives

- Strict markers (A11 i); requiring order (A11 iii). Keeping preambles. Putting
  the builder and the parser in `generator.py`.

### Scope

Pinned by `tests/unit/generation/test_context.py` and
`tests/unit/generation/test_parser.py`, including a test that runs clean parser
output through the validator. The real parse-failure rate is a Stage 9 question.

Introduced in Batch 11, Stage 7.

---

## D-017

Generation stage conventions (SDS §5.10.2, §5.15.3). `GenerateStage`
(`arip/pipeline/stages/generate.py`) receives the session factory, an LLM factory
(`LLMRegistry.get_backend`), the prompt manager, the validator,
`config.llm.generation`, `config.generation.max_retries`, the configured
languages and `pipeline.max_items_per_run`. `container.py` binds everything but
the session factory with `functools.partial`; the orchestrator calls the factory
after the embedding stage has returned. Selection runs once per pass, in one
session, before the model is loaded: `is_eligible(item)` is
`abstract is not None and abstract.strip() != ""`, and
`select_for_generation(items, cap)` walks the D-010 order, skips and counts
ineligible items, selects the first `cap` eligible ones and counts the rest as
beyond the cap. A selected item that fails is not replaced. One
`with llm_factory() as llm:` block covers the pass. Each item runs in its own
`session_scope()` and is re-loaded there by id. An `LLMError` is caught inside
that scope: the item is marked FAILED, the error is returned out of the scope and
raised once the scope has committed, and the pass stops (threshold 1). Any other
exception rolls the item back, leaves it ENRICHED and stops the pass. Every
FAILED transition writes `failed_at_stage='GENERATION'` and
`failure_reason=last_failure_reason`, then calls `deactivate_all_for_item()`;
GENERATED takes `generated_at` from the active row. Counts are logged per
language in `generation_stage_completed`; `stage_metrics` is not written.
`_build_generation()` returns None when `generation.enabled` is false, and then
nothing generation-related is built and the orchestrator logs
`generation_stage_disabled`. When true it builds `LLMRegistry`,
`PromptManager(Path("config/prompts"))`, converts `pipeline.languages` (an
unknown or repeated code is a `ConfigError`) and looks up the `thread` template
for each language.

### Reason

- Q5 (i): an item wrongly left ENRICHED waits one run on its stored score; an
  item wrongly marked FAILED needs a manual re-queue, for which no tool exists
  (TD-027). §5.10 treats an `LLMError` as a hardware or model issue, which the
  next item meets too; OOM from outside pressure on 8 GB persists.
- D-015 requirement 1: returning the error out of the scope is what lets the
  scope commit the attempt rows with the FAILED transition.
- Q8 (b): `is_active` means "selected for review/publishing" (§4.3); a FAILED
  item is neither, and the generator stays unchanged.
- Q2 (a), Q15 (a): with the switch off a run behaves as before Stage 8, and
  `test_orchestrator.py` needs no change. Q3 (a): §5.8 names `config/prompts/`.
  Q6 (a), Q7 (a): a stopped pass is visible as a FAILED run with the error text;
  an exception that is not the item's fault does not fail the item. Q9 (a): one
  timestamp for the item and its active row. Q10 (a): the canonical
  `session_scope()`. Q11 (a): ordering by structure leaves the §5.6 interface
  and `interfaces.py` unchanged. Q12 (a), Q13 (a): the Stage 4 default; the
  column stays a Phase 7 deliverable. Q14 (a): codes are checked only when
  generation is on.

### Rejected Alternatives

- A stop threshold of 2 or 3, or an OOM-aware rule (Q5 ii–iv). A COMPLETED run
  after a stop (Q6 b). Marking an item FAILED for an exception that is not an
  `LLMError` (Q7 b). Keeping the earlier active row of a FAILED item, or
  deactivating at pass start in the generator (Q8 a, c). One session for the
  whole stage, or explicit `commit()` calls (Q10 b, c). `BaseEmbedder.is_loaded`
  with a runtime check (Q11 b). A WARNING line for skipped items (Q12 b).
  Building the registry and templates when generation is off (Q2 b). A config
  field for the template directory (Q3 c). Language validation in the config
  model (Q14 b).

### Scope

`arip/pipeline/stages/generate.py`, the `_generate()` step of
`arip/pipeline/orchestrator.py`, `_build_generation()` and `_languages()` in
`arip/container.py`, and `generation.enabled` in `arip/config.py` and
`config/settings.yaml`. Pinned by `tests/unit/pipeline/test_generate.py`,
`tests/unit/pipeline/test_orchestrator_generation.py`,
`tests/unit/test_container_generation.py` and the appended tests of
`tests/unit/test_config_generation.py` (mutations S1–S10, T1–T11, P1–P6, O1–O4,
W1–W5, C1–C3). The live run is Stage 9.

Introduced in Batch 11, Stage 8.