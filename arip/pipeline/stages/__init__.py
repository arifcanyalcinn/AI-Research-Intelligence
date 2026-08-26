"""
Pipeline stages for ARIP.

Each stage is a plain class constructed with its dependencies (AD-11) and
driven in sequence by PipelineOrchestrator. Stages never manage their own
database session — the orchestrator wraps each stage in a single
``session_scope()`` (SDS §5.14).

Implemented in Batch 7 (SDS §8.2):
  - collect.py — fetch, normalize, exact-deduplicate, persist

Delivered by subsequent batches (SDS §6):
  rank.py, embed.py, generate.py, review.py, publish.py, archive.py
"""
