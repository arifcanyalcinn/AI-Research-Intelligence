"""
Pluggable backends for ARIP.

Two families, each a plugin package with its own registry (SDS §6):

  - ``embeddings/`` — CPU-only sentence embedding (§5.6). Delivered in Batch 9.
  - ``llm/``        — GPU LLM inference (§5.9). Delivered in Phase 4.

Both follow the discovery rule of §1.2 / AD-02: the package ``__init__.py``
imports every backend module explicitly, and the registry then scans
``BaseClass.__subclasses__()``. Per D-004 that scan does not recurse, so every
backend must subclass its ABC **directly** — no shared intermediate base.
"""
