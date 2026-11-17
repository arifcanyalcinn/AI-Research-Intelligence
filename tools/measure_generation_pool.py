"""Read-only measurements for TD-020 and TD-023 (Batch 11, Stage 2).

Operator script, not application code: ``tools/`` is excluded from the ruff gate
(``extend-exclude`` in pyproject.toml, D-007 scope). It never opens the live
database and never writes to any database. It measures a snapshot made with
``tools/measure_ranking_pool.py snapshot``.

Usage (from the repository root, with the package installed)::

    python -I tools/measure_generation_pool.py selection COPY_DB [--out FILE]
    python -I tools/measure_generation_pool.py dedup COPY_DB [--csv FILE] [--out FILE]
    python -I tools/measure_generation_pool.py compare A.csv B.csv [--out FILE]

Every subcommand also takes ``--config PATH`` (default config/settings.yaml).
``--out`` writes the printed report to a new UTF-8 file as well.

selection  (TD-023; needs no optional extra)
    Over ENRICHED rows, per language: NULL counts for the fields generation
    will need; ties in ``importance_score``; how many ties each key of the ruled
    order resolves in turn (importance_score desc, engagement desc, topic desc,
    published_date desc, id asc); whether placing NULL ``published_date`` first
    or last changes anything; the composition of each successive pass of
    ``pipeline.max_items_per_run`` items; and the difference from the id-only
    tie-break that ``measure_ranking_pool.py`` uses for its own simulation.
    It simulates an order; it decides nothing.

dedup  (TD-020; needs the ``embedding`` extra)
    Re-embeds every row the embedding stage processed
    (``embedding_computed_at IS NOT NULL``) with the configured embedding
    backend, on CPU, one text per call as the stage does, using the same text
    (SDS 5.6: title + ". " + abstract). Hugging Face Hub access is switched off
    for the process, so nothing is downloaded: the model must already be cached.
    From the exact pairwise cosine similarities it prints: every stored
    semantic DUPLICATE decision with its similarity; counts per source pair at
    fixed similarity steps; every pair at or above ``--floor`` that involves
    ``huggingface_models`` or ``huggingface_spaces``; the other cross-source
    pairs at or above the floor; a replay of the stage's sequential decision
    under the current rule, compared with the stored statuses; and a replay
    with those two sources excluded from the decision and from the index
    (direction (b) of TD-020), with every decision that would change.
    ``--csv`` writes every pair's similarity to a new file for ``compare``.

    The replay processes rows in the embedding stage's own order: the stage
    fetches RANKED items with ``ORDER BY collected_at`` (``ItemRepository.
    get_by_status``), and the replay uses ``collected_at, id``. SQL leaves the
    order among equal ``collected_at`` values undefined; the ``id`` tiebreak is
    what SQLite returned for that query in a test (index on status, then a
    sort), not a guarantee. The order is not taken from ``embedding_computed_at``,
    which can tie when items are processed within one clock tick (Batch 11
    Stage 2, Windows). Instead the report checks the replay order against it:
    a pair embedded in the opposite order is a contradiction and is listed.
    The replay also assumes the ANN index began empty and was never rebuilt.
    The stage asks an approximate index for the nearest neighbour; the replay
    uses the exact one.
    A replay that does not reproduce the stored statuses is reported, not
    hidden: it means one of those assumptions, or the embedding environment,
    differs from the run that built the pool.

compare
    Compares two ``--csv`` files from ``dedup`` (for example one per virtual
    environment): the largest and mean absolute difference, and every pair that
    is on different sides of ``dedup.semantic_threshold`` in the two files.

Read-only by construction: ``mode=ro`` URI (plus ``immutable=1`` when no
``-wal`` file is present), ``PRAGMA query_only = ON``, SELECT statements only,
refusal to open the configured live database or anything in its directory,
refusal to write a report into that directory or over an existing file, no ANN
index file is read or written, and a SHA-256 of the copy taken before and after
the run.

Exit status: 0 on success; 1 if the copy changed during measurement; 2 on a
usage error or a refused path.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from importlib import metadata
from pathlib import Path

EXCLUDED_SOURCES: frozenset[str] = frozenset({"huggingface_models", "huggingface_spaces"})
"""The sources direction (b) of TD-020 would exclude from semantic deduplication."""

SIMILARITY_STEPS: tuple[float, ...] = (0.80, 0.85, 0.90, 0.92, 0.95)
DEFAULT_FLOOR: float = 0.85
ORDER_KEYS: tuple[str, ...] = ("importance_score", "engagement", "topic", "published_date", "id")
"""The tie-break order ruled in principle on 2026-10-29 (TD-023 a)."""

TITLE_WIDTH: int = 64
LIST_LIMIT: int = 60


class RefusedError(Exception):
    """Raised when a path would make the run unsafe; exit status 2."""


# ---------------------------------------------------------------------------
# Read-only access (copied from tools/measure_ranking_pool.py, ruling Q3)
# ---------------------------------------------------------------------------


def _wal_path(db: Path) -> Path:
    return db.with_name(db.name + "-wal")


def read_only_uri(db: Path) -> str:
    """SQLite URI that cannot write the database file.

    ``immutable=1`` is added only when no ``-wal`` file exists, because an
    immutable open ignores the WAL and would miss committed data held there.
    """
    query = "mode=ro" if _wal_path(db).exists() else "mode=ro&immutable=1"
    return f"{db.resolve().as_uri()}?{query}"


def connect_read_only(db: Path) -> sqlite3.Connection:
    """Open ``db`` read-only, with ``query_only`` as a second guard."""
    conn = sqlite3.connect(read_only_uri(db), uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, directory: Path) -> bool:
    return path.resolve().parent == directory.resolve() or directory.resolve() in (
        path.resolve().parents
    )


# ---------------------------------------------------------------------------
# Settings and path guards
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSettings:
    """The configured values this tool reads; nothing is hard-coded from them."""

    live_db: Path
    threshold: float
    top_n: int
    embeddings: object


def read_settings(config: Path) -> ToolSettings:
    from sqlalchemy.engine import make_url

    from arip.config import load_settings

    settings = load_settings(config)
    return ToolSettings(
        live_db=Path(make_url(settings.database.url).database or ""),
        threshold=settings.dedup.semantic_threshold,
        top_n=settings.pipeline.max_items_per_run,
        embeddings=settings.embeddings,
    )


def guard_copy(copy: Path, live: Path) -> None:
    """Refuse anything that is, or sits beside, the configured live database."""
    if not copy.is_file():
        raise RefusedError(f"copy not found: {copy}")
    if copy.resolve() == live.resolve() or _is_within(copy, live.parent):
        raise RefusedError(
            f"refusing to measure {copy}: it is the configured database or in its "
            f"directory ({live.parent.resolve()}). Measure a snapshot instead."
        )


def guard_output(path: Path | None, live: Path) -> None:
    """Refuse to overwrite a file or to write into the live database's directory."""
    if path is None:
        return
    if path.exists():
        raise RefusedError(f"output file already exists: {path}")
    if not path.parent.is_dir():
        raise RefusedError(f"output directory does not exist: {path.parent}")
    if _is_within(path, live.parent):
        raise RefusedError(
            f"refusing to write {path}: it is in the live database's directory "
            f"({live.parent.resolve()})"
        )


def _short(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= TITLE_WIDTH else text[: TITLE_WIDTH - 1] + "…"


def _fmt_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"


# ---------------------------------------------------------------------------
# selection — TD-023
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelRow:
    id: int
    source_id: str
    language: str
    score: float | None
    engagement: float | None
    topic: float | None
    published: date | None
    has_url: bool
    has_title: bool
    breakdown_ok: bool


@dataclass
class Boundary:
    """What happens at one cutoff (between positions ``position`` and ``position + 1``)."""

    position: int
    score_tie_size: int
    """Rows sharing the importance_score of the last selected row; 1 means no tie."""
    straddles: bool
    """True if the first unselected row has the same importance_score."""
    resolved_by: str | None
    """First key of the ruled order that separates the two rows; None if not tied."""


@dataclass
class LanguageSelection:
    language: str
    total: int
    nulls: dict[str, int]
    by_source: dict[str, int]
    order: list[int]
    """Item ids in the ruled order, NULL published_date placed last."""
    order_null_first: list[int]
    unresolved: list[tuple[str, int, int]]
    """Per key prefix: (last key, rows still tied with another row, tie groups)."""
    score_groups: list[int]
    """Sizes of the importance_score tie groups (size >= 2), largest first."""
    boundaries: list[Boundary]
    passes: list[dict[str, object]]
    null_placement_changes_order: bool
    null_placement_changes_passes: bool
    id_only_top: list[int]
    only_in_ruled: list[int]
    only_in_id_only: list[int]


@dataclass
class SelectionResults:
    top_n: int
    statuses: dict[str, int]
    languages: list[LanguageSelection] = field(default_factory=list)


def _signal(data: object, key: str) -> float | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _as_date(value: object) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(str(value)[:10])


def load_selection_rows(conn: sqlite3.Connection) -> list[SelRow]:
    rows: list[SelRow] = []
    cursor = conn.execute(
        "SELECT id, source_id, language, importance_score, signal_breakdown, "
        "published_date, primary_url, title FROM items WHERE status = 'ENRICHED' ORDER BY id"
    )
    for item_id, source_id, language, score, raw, published, url, title in cursor:
        try:
            data = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            data = None
        rows.append(
            SelRow(
                id=item_id,
                source_id=source_id,
                language=language,
                score=None if score is None else float(score),
                engagement=_signal(data, "engagement"),
                topic=_signal(data, "topic"),
                published=_as_date(published),
                has_url=bool(url),
                has_title=bool(title),
                breakdown_ok=isinstance(data, dict),
            )
        )
    return rows


def order_key(row: SelRow, *, null_date_first: bool) -> tuple[float, float, float, float, int]:
    """Sort key for the ruled order; smaller sorts first.

    A NULL importance_score, engagement or topic sorts last within its key
    (the repository already orders NULL scores last). A NULL published_date is
    placed first or last as asked: its placement is not ruled yet (Q4).
    """
    score = -math.inf if row.score is None else row.score
    engagement = -math.inf if row.engagement is None else row.engagement
    topic = -math.inf if row.topic is None else row.topic
    if row.published is None:
        published = math.inf if null_date_first else -math.inf
    else:
        published = float(row.published.toordinal())
    return (-score, -engagement, -topic, -published, row.id)


def _chunks(order: list[int], size: int) -> list[list[int]]:
    return [order[i : i + size] for i in range(0, len(order), size)]


def measure_language(language: str, rows: list[SelRow], top_n: int) -> LanguageSelection:
    by_id = {r.id: r for r in rows}
    keys = {r.id: order_key(r, null_date_first=False) for r in rows}
    ordered = sorted(rows, key=lambda r: keys[r.id])
    order = [r.id for r in ordered]
    order_first = [r.id for r in sorted(rows, key=lambda r: order_key(r, null_date_first=True))]

    unresolved: list[tuple[str, int, int]] = []
    for length in range(1, len(ORDER_KEYS) + 1):
        groups = Counter(keys[r.id][:length] for r in rows)
        tied = [n for n in groups.values() if n > 1]
        unresolved.append((ORDER_KEYS[length - 1], sum(tied), len(tied)))

    score_counts = Counter(keys[r.id][0] for r in rows)
    score_groups = sorted((n for n in score_counts.values() if n > 1), reverse=True)

    boundaries: list[Boundary] = []
    for position in range(top_n, len(order), top_n):
        last, first_out = keys[order[position - 1]], keys[order[position]]
        straddles = last[0] == first_out[0]
        resolved_by = None
        if straddles:
            for index, name in enumerate(ORDER_KEYS):
                if last[index] != first_out[index]:
                    resolved_by = name
                    break
        boundaries.append(Boundary(position, score_counts[last[0]], straddles, resolved_by))

    passes: list[dict[str, object]] = []
    for number, chunk in enumerate(_chunks(order, top_n), start=1):
        members = [by_id[i] for i in chunk]
        scores = [m.score for m in members if m.score is not None]
        dates = [m.published for m in members if m.published is not None]
        passes.append(
            {
                "pass": number,
                "size": len(chunk),
                "by_source": dict(Counter(m.source_id for m in members)),
                "score_max": max(scores) if scores else None,
                "score_min": min(scores) if scores else None,
                "oldest": min(dates) if dates else None,
                "newest": max(dates) if dates else None,
            }
        )

    id_only = [
        r.id
        for r in sorted(rows, key=lambda r: (-(r.score if r.score is not None else -math.inf), r.id))
    ][:top_n]
    ruled_top = order[:top_n]
    return LanguageSelection(
        language=language,
        total=len(rows),
        nulls={
            "importance_score": sum(r.score is None for r in rows),
            "signal_breakdown (missing or not an object)": sum(not r.breakdown_ok for r in rows),
            "engagement key": sum(r.engagement is None for r in rows),
            "topic key": sum(r.topic is None for r in rows),
            "published_date": sum(r.published is None for r in rows),
            "primary_url": sum(not r.has_url for r in rows),
            "title": sum(not r.has_title for r in rows),
        },
        by_source=dict(Counter(r.source_id for r in rows)),
        order=order,
        order_null_first=order_first,
        unresolved=unresolved,
        score_groups=score_groups,
        boundaries=boundaries,
        passes=passes,
        null_placement_changes_order=order != order_first,
        null_placement_changes_passes=[set(c) for c in _chunks(order, top_n)]
        != [set(c) for c in _chunks(order_first, top_n)],
        id_only_top=id_only,
        only_in_ruled=sorted(set(ruled_top) - set(id_only)),
        only_in_id_only=sorted(set(id_only) - set(ruled_top)),
    )


def measure_selection(conn: sqlite3.Connection, top_n: int) -> SelectionResults:
    statuses = dict(conn.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
    results = SelectionResults(top_n=top_n, statuses=statuses)
    rows = load_selection_rows(conn)
    for language in sorted({r.language for r in rows}):
        subset = [r for r in rows if r.language == language]
        results.languages.append(measure_language(language, subset, top_n))
    return results


def render_selection(res: SelectionResults) -> str:
    out: list[str] = []
    add = out.append
    add("")
    add("SELECTION (TD-023) — ENRICHED rows, simulated order; nothing is decided here")
    add(f"  Rows by status: {_fmt_counts(res.statuses)}")
    add(f"  pipeline.max_items_per_run (N): {res.top_n}")
    add(f"  Ruled order: {', '.join(ORDER_KEYS[:-1])} (all descending), then id ascending")
    if not res.languages:
        add("  No ENRICHED rows.")
    for lang in res.languages:
        add("")
        add(f"Language {lang.language}: {lang.total} ENRICHED rows — {_fmt_counts(lang.by_source)}")
        add("  1. NULL or unusable values")
        for name, count in lang.nulls.items():
            add(f"     {name:<44} {count}")
        add("  2. Ties in importance_score")
        if lang.score_groups:
            add(
                f"     {len(lang.score_groups)} tie groups covering {sum(lang.score_groups)} rows; "
                f"largest {lang.score_groups[0]}; sizes {lang.score_groups[:15]}"
            )
        else:
            add("     none")
        add("  3. Rows still tied with another row after each key of the ruled order")
        for name, tied, groups in lang.unresolved:
            add(f"     after {name:<17} {tied:>4} rows in {groups} groups")
        add(f"  4. Cutoffs (every {res.top_n} rows)")
        if not lang.boundaries:
            add("     none: the pool fits in one pass")
        for b in lang.boundaries:
            if b.straddles:
                add(
                    f"     after position {b.position}: TIE across the cutoff "
                    f"({b.score_tie_size} rows share the score); first separated by {b.resolved_by}"
                )
            else:
                add(f"     after position {b.position}: no tie across the cutoff")
        add("  5. NULL published_date placed first or last")
        add(f"     order changes:            {'YES' if lang.null_placement_changes_order else 'no'}")
        add(f"     any pass's members change: {'YES' if lang.null_placement_changes_passes else 'no'}")
        add("  6. Passes of N in the ruled order (NULL dates last), no new items arriving")
        for p in lang.passes:
            smax, smin = p["score_max"], p["score_min"]
            scores = "n/a" if smax is None else f"{smax:.4f}..{smin:.4f}"
            add(
                f"     pass {p['pass']}: {p['size']:>3} rows, score {scores}, "
                f"published {p['oldest']}..{p['newest']} — {_fmt_counts(p['by_source'])}"
            )
        left = max(lang.total - res.top_n, 0)
        add(f"     left ENRICHED after one pass: {left} (arithmetic on this pool, not a forecast)")
        add("  7. Top N against the id-only tie-break of measure_ranking_pool.py")
        add(f"     only in the ruled top N:   {lang.only_in_ruled or 'none'}")
        add(f"     only in the id-only top N: {lang.only_in_id_only or 'none'}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# dedup — TD-020
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbRow:
    id: int
    source_id: str
    status: str
    title: str | None
    abstract: str | None
    duplicate_of_id: int | None
    is_semantic: bool
    collected_at: str
    embedded_at: str


@dataclass(frozen=True)
class Pair:
    id_a: int
    id_b: int
    similarity: float


@dataclass
class PairStats:
    pairs: int
    at_or_above: list[int]
    maximum: float | None
    max_unlinked: float | None
    """Highest similarity between two items not linked by stored duplicate decisions."""


@dataclass
class DedupResults:
    rows: list[EmbRow]
    threshold: float
    floor: float
    runs: int
    collected_ties: tuple[int, int]
    """(rows sharing a collected_at value with another row, number of such groups)."""
    embedded_ties: tuple[int, int]
    """The same for embedding_computed_at."""
    model: str
    dim: int
    by_status: dict[str, int] = field(default_factory=dict)
    stored: list[tuple[int, int | None, float | None]] = field(default_factory=list)
    """(duplicate id, stored duplicate_of_id, similarity or None if the survivor is absent)."""
    pair_table: dict[tuple[str, str], PairStats] = field(default_factory=dict)
    near_excluded: list[Pair] = field(default_factory=list)
    near_other_cross: list[Pair] = field(default_factory=list)
    replay_current: dict[int, int | None] = field(default_factory=dict)
    mismatches: list[tuple[int, int | None, int | None]] = field(default_factory=list)
    """(item id, stored survivor, replayed survivor) where the two differ."""
    replay_excluded: dict[int, int | None] = field(default_factory=dict)
    changes: list[tuple[int, int | None, int | None]] = field(default_factory=list)
    """(item id, stored survivor, survivor with the two sources excluded)."""
    excluded_pairs_above: list[Pair] = field(default_factory=list)
    """Pairs >= threshold with an excluded source on both sides."""
    lost_cross_pairs: list[Pair] = field(default_factory=list)
    """Pairs >= threshold between an excluded source and any other source."""
    all_pairs: list[Pair] = field(default_factory=list)
    contradictions: list[tuple[int, int]] = field(default_factory=list)
    """(a, b): a comes before b in the replay order but was embedded after b."""


def embedding_text(title: str | None, abstract: str | None) -> str:
    """The text the embedding stage embeds (SDS 5.6; ``EmbedStage._embedding_text``)."""
    return (title or "") + ". " + (abstract or "")


def load_embedded_rows(conn: sqlite3.Connection) -> list[EmbRow]:
    """Every row the embedding stage processed, in the stage's own order.

    ``EmbedStage.run`` processes ``get_by_status(RANKED)``, which orders by
    ``collected_at`` only; ``id`` breaks ties (see the module docstring).
    """
    cursor = conn.execute(
        "SELECT id, source_id, status, title, abstract, duplicate_of_id, "
        "is_semantic_duplicate, collected_at, embedding_computed_at FROM items "
        "WHERE embedding_computed_at IS NOT NULL ORDER BY collected_at, id"
    )
    return [
        EmbRow(i, source, status, title, abstract, dup, bool(semantic), str(coll), str(at))
        for i, source, status, title, abstract, dup, semantic, coll, at in cursor
    ]


def tie_counts(values: Sequence[str]) -> tuple[int, int]:
    """(rows whose value is shared with another row, number of shared values)."""
    shared = [n for n in Counter(values).values() if n > 1]
    return (sum(shared), len(shared))


def order_contradictions(rows: Sequence[EmbRow]) -> list[tuple[int, int]]:
    """Pairs in replay order whose embedding_computed_at is strictly reversed.

    Equal timestamps are not contradictions: they say nothing about order.
    """
    found = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if rows[i].embedded_at > rows[j].embedded_at:
                found.append((rows[i].id, rows[j].id))
    return found


def similarity_matrix(rows: Sequence[EmbRow], embedder: object):
    """Exact cosine similarity between every two rows; also the embedding width."""
    import numpy as np

    with embedder as loaded:  # type: ignore[attr-defined]
        vectors = [
            np.asarray(loaded.embed([embedding_text(r.title, r.abstract)])[0], dtype=np.float32)
            for r in rows
        ]
    matrix = np.vstack(vectors).astype(np.float64)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0.0] = 1.0
    unit = matrix / norms[:, None]
    return unit @ unit.T, int(matrix.shape[1])


def replay(
    sources: Sequence[str], sim, threshold: float, excluded: frozenset[str]
) -> dict[int, int | None]:
    """The embedding stage's sequential decision, on row positions.

    Rows are taken in order. A row from an excluded source is neither checked
    nor indexed. Any other row is compared with its nearest neighbour among the
    rows indexed so far: at or above the threshold it is a duplicate of that
    neighbour and is not indexed; otherwise it is indexed.

    Returns, per position, the survivor's position or None.
    """
    indexed: list[int] = []
    outcome: dict[int, int | None] = {}
    for i, source in enumerate(sources):
        if source in excluded:
            outcome[i] = None
            continue
        if indexed:
            nearest = max(indexed, key=lambda k: (sim[i, k], -k))
            if sim[i, nearest] >= threshold:
                outcome[i] = nearest
                continue
        outcome[i] = None
        indexed.append(i)
    return outcome


def _clusters(rows: Sequence[EmbRow]) -> dict[int, int]:
    """Union of stored duplicate decisions: item id -> cluster representative."""
    parent = {r.id: r.id for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in rows:
        if r.status == "DUPLICATE" and r.is_semantic and r.duplicate_of_id in parent:
            parent[find(r.id)] = find(r.duplicate_of_id)
    return {r.id: find(r.id) for r in rows}


def measure_dedup(
    conn: sqlite3.Connection, embedder: object, threshold: float, floor: float
) -> DedupResults:
    rows = load_embedded_rows(conn)
    runs = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
    res = DedupResults(
        rows=rows,
        threshold=threshold,
        floor=floor,
        runs=runs,
        collected_ties=tie_counts([r.collected_at for r in rows]),
        embedded_ties=tie_counts([r.embedded_at for r in rows]),
        contradictions=order_contradictions(rows),
        model=str(getattr(embedder, "model_name", "?")),
        dim=0,
        by_status=dict(Counter(f"{r.source_id}/{r.status}" for r in rows)),
    )
    if not rows:
        return res
    sim, res.dim = similarity_matrix(rows, embedder)
    position = {r.id: i for i, r in enumerate(rows)}
    sources = [r.source_id for r in rows]
    cluster = _clusters(rows)

    stored_survivor: dict[int, int | None] = {}
    for r in rows:
        is_dup = r.status == "DUPLICATE" and r.is_semantic
        stored_survivor[r.id] = r.duplicate_of_id if is_dup else None
        if is_dup:
            other = position.get(r.duplicate_of_id) if r.duplicate_of_id is not None else None
            value = None if other is None else float(sim[position[r.id], other])
            res.stored.append((r.id, r.duplicate_of_id, value))

    table: dict[tuple[str, str], PairStats] = {}
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            value = float(sim[i, j])
            a, b = rows[i], rows[j]
            pair = Pair(min(a.id, b.id), max(a.id, b.id), value)
            res.all_pairs.append(pair)
            key = tuple(sorted((a.source_id, b.source_id)))
            stats = table.setdefault(key, PairStats(0, [0] * len(SIMILARITY_STEPS), None, None))
            stats.pairs += 1
            for index, step in enumerate(SIMILARITY_STEPS):
                if value >= step:
                    stats.at_or_above[index] += 1
            stats.maximum = value if stats.maximum is None else max(stats.maximum, value)
            if cluster[a.id] != cluster[b.id]:
                stats.max_unlinked = (
                    value if stats.max_unlinked is None else max(stats.max_unlinked, value)
                )
            in_excluded = (a.source_id in EXCLUDED_SOURCES, b.source_id in EXCLUDED_SOURCES)
            if value >= floor:
                if any(in_excluded):
                    res.near_excluded.append(pair)
                elif a.source_id != b.source_id:
                    res.near_other_cross.append(pair)
            if value >= threshold:
                if all(in_excluded):
                    res.excluded_pairs_above.append(pair)
                elif any(in_excluded):
                    res.lost_cross_pairs.append(pair)
    res.pair_table = dict(sorted(table.items()))
    for pairs in (
        res.near_excluded,
        res.near_other_cross,
        res.excluded_pairs_above,
        res.lost_cross_pairs,
    ):
        pairs.sort(key=lambda p: (-p.similarity, p.id_a, p.id_b))
    res.all_pairs.sort(key=lambda p: (p.id_a, p.id_b))

    def by_id(outcome: dict[int, int | None]) -> dict[int, int | None]:
        return {rows[i].id: (None if j is None else rows[j].id) for i, j in outcome.items()}

    res.replay_current = by_id(replay(sources, sim, threshold, frozenset()))
    res.replay_excluded = by_id(replay(sources, sim, threshold, EXCLUDED_SOURCES))
    for r in rows:
        if res.replay_current[r.id] != stored_survivor[r.id]:
            res.mismatches.append((r.id, stored_survivor[r.id], res.replay_current[r.id]))
        if res.replay_excluded[r.id] != stored_survivor[r.id]:
            res.changes.append((r.id, stored_survivor[r.id], res.replay_excluded[r.id]))
    return res


def _environment() -> str:
    parts = [f"python {sys.version.split()[0]}"]
    for name in ("torch", "transformers", "sentence-transformers", "numpy"):
        try:
            parts.append(f"{name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            parts.append(f"{name} not installed")
    return ", ".join(parts)


def render_dedup(res: DedupResults) -> str:
    by_id = {r.id: r for r in res.rows}

    def label(item_id: int | None) -> str:
        row = by_id.get(item_id) if item_id is not None else None
        if row is None:
            return f"#{item_id} (not among the embedded rows)"
        return f"#{row.id} [{row.source_id}/{row.status}] {_short(row.title)}"

    def pair_lines(pairs: list[Pair], indent: str = "     ") -> list[str]:
        lines = []
        for p in pairs[:LIST_LIMIT]:
            lines.append(f"{indent}{p.similarity:.4f}  {label(p.id_a)}")
            lines.append(f"{indent}        {label(p.id_b)}")
        if len(pairs) > LIST_LIMIT:
            lines.append(f"{indent}… {len(pairs) - LIST_LIMIT} more (all pairs are in --csv)")
        return lines or [f"{indent}none"]

    def survivor(item_id: int | None) -> str:
        return "not a duplicate" if item_id is None else f"duplicate of {label(item_id)}"

    out: list[str] = []
    add = out.append
    add("")
    add("SEMANTIC DEDUPLICATION (TD-020) — measured, nothing is decided here")
    add(f"  Environment: {_environment()}")
    add(f"  Interpreter prefix: {sys.prefix}")
    add(f"  Embedding model: {res.model} (width {res.dim}), one text per call, Hub offline")
    add(f"  dedup.semantic_threshold: {res.threshold}; list floor: {res.floor}")
    add(f"  Rows the embedding stage processed: {len(res.rows)} — {_fmt_counts(res.by_status)}")
    add(f"  Pipeline runs in this database: {res.runs}")
    if not res.rows:
        add("  Nothing to measure.")
        return "\n".join(out)

    add("")
    add("1. Stored semantic DUPLICATE decisions, re-measured")
    if not res.stored:
        add("     none")
    for item_id, survivor_id, value in res.stored:
        shown = "   n/a" if value is None else f"{value:.4f}"
        flag = "" if value is None or value >= res.threshold else "  BELOW THRESHOLD HERE"
        add(f"     {shown}  {label(item_id)}{flag}")
        add(f"          -> {label(survivor_id)}")

    add("")
    steps = "  ".join(f">={s:.2f}" for s in SIMILARITY_STEPS)
    add("2. Pairs per source pair")
    add(f"     {'source pair':<44} {'pairs':>6}  {steps}     max  max unlinked")
    for (a, b), s in res.pair_table.items():
        counts = "  ".join(f"{n:>6}" for n in s.at_or_above)
        maximum = "   n/a" if s.maximum is None else f"{s.maximum:.4f}"
        unlinked = "   n/a" if s.max_unlinked is None else f"{s.max_unlinked:.4f}"
        add(f"     {a + ' × ' + b:<44} {s.pairs:>6}  {counts}  {maximum}  {unlinked}")
    add("     'max unlinked': highest similarity between two items that no stored")
    add("     duplicate decision connects (how close the nearest non-duplicate is).")

    names = " or ".join(sorted(EXCLUDED_SOURCES))
    add("")
    add(f"3. Pairs at or above {res.floor} involving {names}: {len(res.near_excluded)}")
    out.extend(pair_lines(res.near_excluded))
    add("")
    add(
        f"4. Other cross-source pairs at or above {res.floor} "
        f"(neither item from those two sources): {len(res.near_other_cross)}"
    )
    out.extend(pair_lines(res.near_other_cross))

    add("")
    add("5. Replay under the current rule, against the stored statuses")
    add("     replay order: collected_at, then id (the embedding stage's own query order)")
    rows_c, groups_c = res.collected_ties
    rows_e, groups_e = res.embedded_ties
    add(
        f"     collected_at ties: {rows_c} rows in {groups_c} groups"
        + ("" if not rows_c else " (order among them is by id: SQLite's observed order, not guaranteed)")
    )
    add(f"     embedding_computed_at ties: {rows_e} rows in {groups_e} groups (not used for ordering)")
    if res.contradictions:
        add(
            f"     ORDER CONTRADICTIONS: {len(res.contradictions)} pairs were embedded in the "
            "opposite order to the replay"
        )
        for a, b in res.contradictions[:LIST_LIMIT]:
            add(f"     {label(a)}")
            add(f"          replayed before, embedded after: {label(b)}")
    else:
        add("     order contradictions with embedding_computed_at: none")
    if res.runs != 1:
        add(f"     note: {res.runs} pipeline runs; the replay assumes the index began empty")
        add("     and was never rebuilt.")
    if res.mismatches:
        add(f"     REPLAY DOES NOT REPRODUCE THE STORED STATUSES: {len(res.mismatches)} rows differ")
        for item_id, was, now in res.mismatches[:LIST_LIMIT]:
            add(f"     {label(item_id)}")
            add(f"          stored:   {survivor(was)}")
            add(f"          replayed: {survivor(now)}")
        add("     Sections 6 and 7 are then indicative only.")
    else:
        add(f"     reproduces all {len(res.rows)} stored decisions exactly")

    dup_now = sum(1 for v in res.replay_excluded.values() if v is not None)
    add("")
    add(f"6. Replay with {' and '.join(sorted(EXCLUDED_SOURCES))} excluded from the decision and the index")
    add(f"     duplicates: {len(res.stored)} stored -> {dup_now} replayed; decisions that change: {len(res.changes)}")
    for item_id, was, now in res.changes[:LIST_LIMIT]:
        add(f"     {label(item_id)}")
        add(f"          stored:        {survivor(was)}")
        add(f"          with exclusion: {survivor(now)}")

    add("")
    add(f"7. What the exclusion stops catching (pairs at or above {res.threshold})")
    add(f"   a. both items from the excluded sources: {len(res.excluded_pairs_above)}")
    out.extend(pair_lines(res.excluded_pairs_above, "      "))
    add(f"   b. one item from an excluded source, one from another source: {len(res.lost_cross_pairs)}")
    out.extend(pair_lines(res.lost_cross_pairs, "      "))
    add("   Whether each pair is a real duplicate is a judgement for the reviewer.")
    return "\n".join(out)


def write_pairs_csv(path: Path, res: DedupResults) -> None:
    source = {r.id: r.source_id for r in res.rows}
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id_a", "id_b", "source_a", "source_b", "similarity"])
        for p in res.all_pairs:
            writer.writerow([p.id_a, p.id_b, source[p.id_a], source[p.id_b], f"{p.similarity:.6f}"])


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


@dataclass
class CompareResults:
    common: int
    only_a: int
    only_b: int
    max_diff: float
    max_pair: tuple[int, int] | None
    mean_diff: float
    crossings: list[tuple[int, int, float, float]]


def read_pairs_csv(path: Path) -> dict[tuple[int, int], float]:
    if not path.is_file():
        raise RefusedError(f"file not found: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        try:
            return {(int(r["id_a"]), int(r["id_b"])): float(r["similarity"]) for r in reader}
        except (KeyError, TypeError, ValueError) as exc:
            raise RefusedError(f"{path} is not a pairs file written by dedup --csv: {exc}") from exc


def compare_pairs(
    a: dict[tuple[int, int], float], b: dict[tuple[int, int], float], threshold: float
) -> CompareResults:
    common = sorted(set(a) & set(b))
    diffs = [(abs(a[k] - b[k]), k) for k in common]
    worst = max(diffs) if diffs else (0.0, None)
    return CompareResults(
        common=len(common),
        only_a=len(set(a) - set(b)),
        only_b=len(set(b) - set(a)),
        max_diff=worst[0],
        max_pair=worst[1],
        mean_diff=sum(d for d, _ in diffs) / len(diffs) if diffs else 0.0,
        crossings=[
            (k[0], k[1], a[k], b[k]) for k in common if (a[k] >= threshold) != (b[k] >= threshold)
        ],
    )


def render_compare(res: CompareResults, threshold: float, a: Path, b: Path) -> str:
    out = [
        "",
        "COMPARE — two dedup --csv files",
        f"  A: {a.resolve()}",
        f"  B: {b.resolve()}",
        f"  Pairs in both: {res.common}; only in A: {res.only_a}; only in B: {res.only_b}",
        f"  Largest absolute difference: {res.max_diff:.6f}"
        + (f" (pair {res.max_pair[0]}, {res.max_pair[1]})" if res.max_pair else ""),
        f"  Mean absolute difference:    {res.mean_diff:.6f}",
        f"  Pairs on different sides of the threshold {threshold}: {len(res.crossings)}",
    ]
    for id_a, id_b, in_a, in_b in res.crossings[:LIST_LIMIT]:
        out.append(f"     pair {id_a}, {id_b}: A {in_a:.6f}, B {in_b:.6f}")
    out.append("  Similarities in the files are rounded to 6 decimals.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _emit(text: str, out: Path | None) -> None:
    print(text)
    if out is not None:
        with out.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")


def _quiet_logging() -> None:
    """Keep the application's info logs out of the report; errors still show."""
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))


def _measured(copy: Path, body, out: Path | None) -> int:
    """Run ``body(conn)`` on the copy between two SHA-256 readings and report."""
    before = sha256_of(copy)
    conn = connect_read_only(copy)
    try:
        text = body(conn)
    finally:
        conn.close()
    after = sha256_of(copy)
    unchanged = after == before
    header = f"Copy: {copy.resolve()}\nSHA-256 before: {before}"
    footer = f"\nSHA-256 after:  {after}  ({'unchanged' if unchanged else 'CHANGED'})"
    _emit(header + "\n" + text + "\n" + footer, out)
    return 0 if unchanged else 1


def run_selection(copy: Path, config: Path, out: Path | None = None) -> int:
    settings = read_settings(config)
    guard_copy(copy, settings.live_db)
    guard_output(out, settings.live_db)
    return _measured(
        copy, lambda conn: render_selection(measure_selection(conn, settings.top_n)), out
    )


def run_dedup(
    copy: Path,
    config: Path,
    out: Path | None = None,
    csv_path: Path | None = None,
    floor: float = DEFAULT_FLOOR,
    embedder: object | None = None,
) -> int:
    """``embedder`` is for the check harness; by default the configured backend is used."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    settings = read_settings(config)
    guard_copy(copy, settings.live_db)
    guard_output(out, settings.live_db)
    guard_output(csv_path, settings.live_db)
    if out is not None and csv_path is not None and out.resolve() == csv_path.resolve():
        raise RefusedError("--out and --csv name the same file")
    _quiet_logging()
    if embedder is None:
        from arip.backends.embeddings.registry import EmbeddingRegistry

        embedder = EmbeddingRegistry(settings.embeddings).get_backend()
    holder: list[DedupResults] = []

    def body(conn: sqlite3.Connection) -> str:
        holder.append(measure_dedup(conn, embedder, settings.threshold, floor))
        return render_dedup(holder[0])

    code = _measured(copy, body, out)
    if csv_path is not None:
        write_pairs_csv(csv_path, holder[0])
        print(f"Pairs written: {csv_path.resolve()} ({len(holder[0].all_pairs)} pairs)")
    return code


def run_compare(a: Path, b: Path, config: Path, out: Path | None = None) -> int:
    settings = read_settings(config)
    guard_output(out, settings.live_db)
    res = compare_pairs(read_pairs_csv(a), read_pairs_csv(b), settings.threshold)
    _emit(render_compare(res, settings.threshold, a, b), out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="measure_generation_pool",
        description="Read-only measurements for TD-020 (dedup) and TD-023 (selection).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=Path("config/settings.yaml"))
    common.add_argument("--out", type=Path, default=None, help="also write the report here")
    sel = sub.add_parser("selection", parents=[common], help="TD-023: order, ties, passes")
    sel.add_argument("copy", type=Path)
    ded = sub.add_parser("dedup", parents=[common], help="TD-020: pairwise similarities")
    ded.add_argument("copy", type=Path)
    ded.add_argument("--csv", type=Path, default=None, help="write every pair here")
    ded.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    cmp_ = sub.add_parser("compare", parents=[common], help="compare two dedup --csv files")
    cmp_.add_argument("a", type=Path)
    cmp_.add_argument("b", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "selection":
            return run_selection(args.copy, args.config, args.out)
        if args.command == "dedup":
            return run_dedup(args.copy, args.config, args.out, args.csv, args.floor)
        return run_compare(args.a, args.b, args.config, args.out)
    except RefusedError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
