"""Read-only measurement of the ranking pool (Batch 10, Stages 2 and 4).

Operator script, not application code: ``tools/`` is excluded from the ruff gate
(``extend-exclude`` in pyproject.toml, D-007 scope). It never opens the live
database for measurement and never writes to any database except the new
snapshot file it is asked to create.

Usage (from the repository root, with the package installed)::

    python -I tools/measure_ranking_pool.py snapshot SOURCE_DB COPY_DB
    python -I tools/measure_ranking_pool.py measure COPY_DB [--config PATH]

snapshot
    Copies SOURCE_DB (normally data/arip.db) to COPY_DB with SQLite's backup
    API, then switches the copy to rollback-journal mode so it is one
    self-contained file. Run it while no ``arip run`` process is active.
    The source is opened read-only: with ``mode=ro&immutable=1`` when no
    ``-wal`` file exists beside it (nothing is created next to the live
    database), otherwise with ``mode=ro`` so that committed transactions still
    held in the WAL are included. COPY_DB must not exist and must not be in the
    source database's directory. Prints the copy's SHA-256.

measure
    Prints the "before" figures for the copy: row counts by source and status;
    per-source means of the four ranking signals over ENRICHED rows; the arxiv
    FILTERED distribution; GitHub topic coverage and the 20 most frequent
    topics; and a top-50 re-scoring sweep over arxiv engagement values.
    Read-only by construction: ``mode=ro`` URI (plus ``immutable=1`` when no
    ``-wal`` file is present), ``PRAGMA query_only = ON``, SELECT statements
    only, refusal to open the configured live database or anything in its
    directory, and a SHA-256 of the copy taken before and after the run.

Fallback, if the snapshot subcommand cannot be used: stop every ARIP process,
then copy ``arip.db`` TOGETHER WITH ``arip.db-wal`` and ``arip.db-shm``
whenever those files exist, to a directory outside data/. Never copy
``arip.db`` alone while an ``arip.db-wal`` file exists: committed data that
has not yet been checkpointed lives only in the WAL, and a database-only copy
silently omits it.

Exit status: 0 on success; 1 if an integrity check fails or the copy changed
during measurement; 2 on a usage error or a refused path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

SIGNALS: tuple[str, ...] = ("recency", "authority", "engagement", "topic")
"""The four ranking signals stored in ``items.signal_breakdown`` (SDS §5.5)."""

METRICLESS_SOURCES: frozenset[str] = frozenset({"arxiv"})
"""Sources whose engagement the sweep replaces (ruling A2: arxiv only)."""

SWEEP: tuple[float, ...] = (0.0, 0.30, 0.35, 0.40, 0.50)
"""Arxiv engagement values simulated; 0.0 reproduces the current scorer."""

SIMULATION_VALUE: float = 0.35
"""The value ruled for TD-016 (§5.5.2), used for the FILTERED would-pass count."""

TOP_N: int = 50
TOP_TOPICS: int = 20
BUCKET: float = 0.05
INTEGRITY_TOLERANCE: float = 1e-9
NEAR_TIE: float = 0.00005
"""Scores within this distance of rank 50 are equal when rounded to 4 decimals."""


class RefusedError(Exception):
    """Raised when a path would make the run unsafe; exit status 2."""


# ---------------------------------------------------------------------------
# Read-only access
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
# snapshot
# ---------------------------------------------------------------------------


def snapshot(source: Path, dest: Path) -> str:
    """Copy ``source`` to the new file ``dest``; return the copy's SHA-256."""
    if not source.is_file():
        raise RefusedError(f"source database not found: {source}")
    if dest.exists():
        raise RefusedError(f"destination already exists: {dest}")
    if not dest.parent.is_dir():
        raise RefusedError(f"destination directory does not exist: {dest.parent}")
    if _is_within(dest, source.parent):
        raise RefusedError(
            f"destination must be outside the source directory {source.parent.resolve()}"
        )
    src = connect_read_only(source)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode = DELETE")
            check = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if check != "ok":
        raise RuntimeError(f"snapshot failed quick_check: {check}")
    return sha256_of(dest)


# ---------------------------------------------------------------------------
# measure — computations
# ---------------------------------------------------------------------------


@dataclass
class Weights:
    recency: float
    authority: float
    engagement: float
    topic: float

    def score(self, signals: dict[str, float]) -> float:
        """Composite score exactly as ``Scorer`` computes it, clamped to [0, 1]."""
        total = (
            self.recency * signals["recency"]
            + self.authority * signals["authority"]
            + self.engagement * signals["engagement"]
            + self.topic * signals["topic"]
        )
        return max(0.0, min(1.0, total))


@dataclass
class PoolRow:
    item_id: int
    source_id: str
    stored_score: float | None
    signals: dict[str, float]


@dataclass
class Results:
    status_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    enriched: dict[str, dict] = field(default_factory=dict)
    arxiv_filtered: dict = field(default_factory=dict)
    github: dict = field(default_factory=dict)
    simulation: dict[float, dict] = field(default_factory=dict)
    integrity_ok: bool = True


def _parse_signals(raw: str | None) -> dict[str, float] | None:
    """The four signals from a stored breakdown, or None if unusable."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, float] = {}
    for key in SIGNALS:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        out[key] = float(value)
    return out


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile of a sorted, non-empty sequence."""
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    fraction = position - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction


def _as_date(value: object) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(str(value)[:10])


def measure(conn: sqlite3.Connection, weights: Weights, min_score: float) -> Results:
    """Compute every figure. Issues SELECT statements only."""
    res = Results()

    # 0. Context: every row by source and status.
    for source_id, status, count in conn.execute(
        "SELECT source_id, status, COUNT(*) FROM items "
        "GROUP BY source_id, status ORDER BY source_id, status"
    ):
        res.status_counts[(source_id, status)] = count

    # 1. ENRICHED pool.
    pool: list[PoolRow] = []
    rows_by_source: Counter[str] = Counter()
    excluded_by_source: Counter[str] = Counter()
    topics_raw: list[tuple[int, str | None]] = []
    for item_id, source_id, stored, breakdown, topics in conn.execute(
        "SELECT id, source_id, importance_score, signal_breakdown, topics "
        "FROM items WHERE status = 'ENRICHED' ORDER BY id"
    ):
        rows_by_source[source_id] += 1
        if source_id == "github_trending":
            topics_raw.append((item_id, topics))
        signals = _parse_signals(breakdown)
        if signals is None:
            excluded_by_source[source_id] += 1
            continue
        pool.append(PoolRow(item_id, source_id, stored, signals))

    for source_id in sorted(rows_by_source):
        members = [row for row in pool if row.source_id == source_id]
        entry: dict = {
            "rows": rows_by_source[source_id],
            "valid": len(members),
            "excluded": excluded_by_source[source_id],
        }
        if members:
            n = len(members)
            entry["mean"] = {k: sum(r.signals[k] for r in members) / n for k in SIGNALS}
            entry["zero_engagement_pct"] = (
                100.0 * sum(1 for r in members if r.signals["engagement"] == 0.0) / n
            )
            entry["zero_topic_pct"] = (
                100.0 * sum(1 for r in members if r.signals["topic"] == 0.0) / n
            )
            diffs = [
                abs(weights.score(r.signals) - r.stored_score)
                for r in members
                if r.stored_score is not None
            ]
            entry["missing_score"] = sum(1 for r in members if r.stored_score is None)
            entry["max_score_diff"] = max(diffs) if diffs else 0.0
            if entry["max_score_diff"] > INTEGRITY_TOLERANCE or entry["missing_score"]:
                res.integrity_ok = False
        res.enriched[source_id] = entry

    # 2. arxiv FILTERED.
    filtered = conn.execute(
        "SELECT id, importance_score, published_date, updated_at FROM items "
        "WHERE source_id = 'arxiv' AND status = 'FILTERED' ORDER BY importance_score, id"
    ).fetchall()
    scores = sorted(row[1] for row in filtered if row[1] is not None)
    delta = weights.engagement * SIMULATION_VALUE
    ages: list[int] = []
    no_date = 0
    for _, _, published, updated in filtered:
        published_on, filtered_on = _as_date(published), _as_date(updated)
        if published_on is None or filtered_on is None:
            no_date += 1
        else:
            ages.append((filtered_on - published_on).days)
    ages.sort()
    histogram: list[tuple[float, int]] = []
    if scores:
        index = [math.floor(round(s / BUCKET, 9)) for s in scores]
        top_index = math.floor(round(min_score / BUCKET, 9))
        if round(top_index * BUCKET, 9) == round(min_score, 9):
            top_index -= 1  # min_score is a bucket edge; FILTERED is strictly below it
        top_index = max(top_index, max(index))
        histogram = [
            (round(i * BUCKET, 2), index.count(i)) for i in range(min(index), top_index + 1)
        ]
    res.arxiv_filtered = {
        "count": len(filtered),
        "null_score": len(filtered) - len(scores),
        "quantiles": (
            tuple(_quantile(scores, q) for q in (0.0, 0.25, 0.5, 0.75, 1.0)) if scores else None
        ),
        "histogram": histogram,
        "age": (ages[0], _quantile(ages, 0.5), ages[-1]) if ages else None,
        "no_date": no_date,
        "delta": delta,
        "would_pass": sum(1 for s in scores if s + delta >= min_score),
    }

    # 3. GitHub topics (ENRICHED rows).
    with_topics = 0
    unparseable = 0
    topic_counts: Counter[str] = Counter()
    for _, raw in topics_raw:
        if raw is None:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            unparseable += 1
            continue
        if isinstance(parsed, list) and parsed:
            with_topics += 1
            topic_counts.update({str(t) for t in parsed})
        elif not isinstance(parsed, list):
            unparseable += 1
    rows = len(topics_raw)
    res.github = {
        "rows": rows,
        "with_topics": with_topics,
        "share_pct": 100.0 * with_topics / rows if rows else 0.0,
        "unparseable": unparseable,
        "top_topics": sorted(topic_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_TOPICS],
    }

    # 4. Top-N sweep over arxiv engagement.
    sources = sorted({row.source_id for row in pool})
    for value in SWEEP:
        ranked = []
        for row in pool:
            signals = dict(row.signals)
            if row.source_id in METRICLESS_SOURCES:
                signals["engagement"] = value
            ranked.append((weights.score(signals), row.item_id, row.source_id))
        ranked.sort(key=lambda t: (-t[0], t[1]))
        top = ranked[:TOP_N]
        counts = Counter(source for _, _, source in top)
        cutoff = top[-1][0] if top else None
        res.simulation[value] = {
            "counts": {source: counts.get(source, 0) for source in sources},
            "rank_n": cutoff,
            "rank_n_plus_1": ranked[TOP_N][0] if len(ranked) > TOP_N else None,
            "exact_ties": sum(1 for s, _, _ in ranked if s == cutoff) if top else 0,
            "near_ties": sum(1 for s, _, _ in ranked if abs(s - cutoff) < NEAR_TIE) if top else 0,
        }
    return res


# ---------------------------------------------------------------------------
# measure — output
# ---------------------------------------------------------------------------


def render(res: Results, weights: Weights, min_score: float) -> str:
    out: list[str] = []
    w = out.append
    w(
        f"Weights: recency {weights.recency}  authority {weights.authority}  "
        f"engagement {weights.engagement}  topic {weights.topic}   min_score {min_score}"
    )

    w("\n[0] Rows by source and status")
    for (source_id, status), count in sorted(res.status_counts.items()):
        w(f"  {source_id:<22}{status:<16}{count:>6}")

    w("\n[1] ENRICHED rows per source")
    w(
        f"  {'SOURCE':<22}{'ROWS':>5}{'USED':>5}{'EXCL':>5}"
        f"{'REC':>8}{'AUT':>8}{'ENG':>8}{'TOP':>8}{'%0-ENG':>8}{'%0-TOP':>8}{'MAXDIFF':>10}"
    )
    for source_id, e in res.enriched.items():
        line = f"  {source_id:<22}{e['rows']:>5}{e['valid']:>5}{e['excluded']:>5}"
        if e["valid"]:
            m = e["mean"]
            line += (
                f"{m['recency']:>8.3f}{m['authority']:>8.3f}{m['engagement']:>8.3f}{m['topic']:>8.3f}"
                f"{e['zero_engagement_pct']:>8.1f}{e['zero_topic_pct']:>8.1f}{e['max_score_diff']:>10.1e}"
            )
            if e["missing_score"]:
                line += f"  ({e['missing_score']} without importance_score)"
        w(line)
    w(
        "  Integrity (stored importance_score = weights x breakdown, "
        f"tolerance {INTEGRITY_TOLERANCE:g}): {'OK' if res.integrity_ok else 'FAILED'}"
    )

    f = res.arxiv_filtered
    w("\n[2] arxiv FILTERED")
    w(
        f"  count: {f['count']}"
        + (f"  (without importance_score: {f['null_score']})" if f["null_score"] else "")
    )
    if f["quantiles"]:
        q = f["quantiles"]
        w(
            f"  score min {q[0]:.4f}  p25 {q[1]:.4f}  median {q[2]:.4f}  "
            f"p75 {q[3]:.4f}  max {q[4]:.4f}"
        )
        for low, n in f["histogram"]:
            w(f"    [{low:.2f}, {low + BUCKET:.2f})  {n}")
    if f["age"]:
        a = f["age"]
        w(f"  age at filtering (days): min {a[0]}  median {a[1]:g}  max {a[2]}")
    if f["no_date"]:
        w(f"  without published_date or updated_at: {f['no_date']}")
    w(
        f"  would pass at arxiv engagement {SIMULATION_VALUE} "
        f"(stored + {f['delta']:.4f} >= {min_score}): {f['would_pass']} of {f['count'] - f['null_score']}"
    )

    g = res.github
    w("\n[3] GitHub topics (ENRICHED github_trending)")
    w(f"  rows {g['rows']}  with non-empty topics {g['with_topics']}  ({g['share_pct']:.1f}%)")
    if g["unparseable"]:
        w(f"  unparseable topics values: {g['unparseable']}")
    w(f"  top {TOP_TOPICS} topics (items carrying the topic):")
    for topic, n in g["top_topics"]:
        w(f"    {n:>4}  {topic}")

    w(f"\n[4] Top-{TOP_N} sweep: arxiv engagement = v (other sources unchanged)")
    sources = sorted(next(iter(res.simulation.values()))["counts"]) if res.simulation else []
    w(
        "  "
        + f"{'v':>5}"
        + "".join(f"{s:>20}" for s in sources)
        + f"{'rank50':>9}{'rank51':>9}{'ties':>6}{'near':>6}"
    )
    for value, s in res.simulation.items():
        r50 = f"{s['rank_n']:.4f}" if s["rank_n"] is not None else "-"
        r51 = f"{s['rank_n_plus_1']:.4f}" if s["rank_n_plus_1"] is not None else "-"
        w(
            "  "
            + f"{value:>5.2f}"
            + "".join(f"{s['counts'][src]:>20}" for src in sources)
            + f"{r50:>9}{r51:>9}{s['exact_ties']:>6}{s['near_ties']:>6}"
        )
    w("  ties: rows whose score equals the rank-50 score; near: within 0.00005 of it")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _live_database(config: Path) -> tuple[Weights, float, Path]:
    from sqlalchemy.engine import make_url

    from arip.config import load_settings

    settings = load_settings(config)
    ranking = settings.ranking
    weights = Weights(
        ranking.weights.recency,
        ranking.weights.authority,
        ranking.weights.engagement,
        ranking.weights.topic,
    )
    live = Path(make_url(settings.database.url).database or "")
    return weights, ranking.min_score, live


def run_measure(copy: Path, config: Path) -> int:
    if not copy.is_file():
        raise RefusedError(f"copy not found: {copy}")
    weights, min_score, live = _live_database(config)
    if copy.resolve() == live.resolve() or _is_within(copy, live.parent):
        raise RefusedError(
            f"refusing to measure {copy}: it is the configured database or in its "
            f"directory ({live.parent.resolve()}). Measure a snapshot instead."
        )
    before = sha256_of(copy)
    print(f"Copy: {copy.resolve()}")
    print(f"SHA-256 before: {before}")
    conn = connect_read_only(copy)
    try:
        res = measure(conn, weights, min_score)
    finally:
        conn.close()
    print(render(res, weights, min_score))
    after = sha256_of(copy)
    unchanged = after == before
    print(f"\nSHA-256 after:  {after}  ({'unchanged' if unchanged else 'CHANGED'})")
    return 0 if unchanged and res.integrity_ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="measure_ranking_pool",
        description="Read-only measurement of the ARIP ranking pool.",
        epilog=(
            "Fallback copy: stop ARIP, then copy arip.db together with arip.db-wal "
            "and arip.db-shm (when present) outside data/. Never copy arip.db alone "
            "while arip.db-wal exists."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="copy a database with the SQLite backup API")
    snap.add_argument("source", type=Path)
    snap.add_argument("dest", type=Path)
    meas = sub.add_parser("measure", help="measure a snapshot (read-only)")
    meas.add_argument("copy", type=Path)
    meas.add_argument("--config", type=Path, default=Path("config/settings.yaml"))
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            digest = snapshot(args.source, args.dest)
            print(f"Snapshot written: {args.dest.resolve()}")
            print(f"SHA-256: {digest}")
            return 0
        return run_measure(args.copy, args.config)
    except RefusedError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
