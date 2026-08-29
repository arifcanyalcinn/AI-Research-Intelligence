"""
Ranking for ARIP.

Contains the composite importance scorer specified in SDS §5.5 and fixed by
§5.5.1. Pure Python arithmetic — no database access, no HTTP, no ML, and no
dependency beyond the standard library (§5.5: "Dependencies: None (pure Python
arithmetic, no imports beyond `math`)").

The stage that applies these scores to items lives in
`arip/pipeline/stages/rank.py`.
"""
