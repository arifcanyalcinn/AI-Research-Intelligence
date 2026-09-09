"""
Duplicate detection for ARIP (SDS §5.7).

§5.7 describes two mechanisms, and only one of them lives here:

  - **Exact deduplication** is a database concern, not a module. §5.7 specifies
    it as the ``UNIQUE(content_hash)`` constraint on ``items``: an
    ``IntegrityError`` from SQLAlchemy *is* the duplicate detection, and
    ``CollectStage`` already discards the row before insertion (Batch 7). There
    is deliberately no ``exact.py`` — a module here would either duplicate that
    constraint in Python, where it could disagree with the database, or wrap a
    single ``except IntegrityError`` in a class.

  - **Semantic deduplication** is ``semantic.SemanticDeduplicator``: an ANN
    similarity search over a persisted ``usearch`` index.

This package holds no registry and no plugin discovery — there is one
deduplicator, named by the SDS, not a family of backends. ``__init__.py`` is
therefore documentation only, and imports nothing: ``semantic`` reaches for
``usearch`` and ``numpy``, which arrive with the optional ``embedding`` extra,
so importing this package must stay free.
"""
