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

This decision is authorized by the Batch 5 SDS amendment.