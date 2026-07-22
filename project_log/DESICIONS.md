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

## Current Phase
Phase 1

## Current Batch
Batchpdate after phase 1 batch 2 

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