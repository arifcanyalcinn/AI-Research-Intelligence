"""Blind human-relevance evaluation of the ranked pool (Batch 11, Stage 3b; TD-025).

Operator script, not application code: ``tools/`` is excluded from the ruff gate
(``extend-exclude`` in pyproject.toml, D-007 scope). It never opens the live
database and never writes to any database. It reads a snapshot made with
``tools/measure_ranking_pool.py snapshot``.

Usage (from the repository root, with the package installed)::

    python -I tools/evaluate_relevance.py sheet COPY_DB --out-dir DIR [--seed N]
    python -I tools/evaluate_relevance.py analyze COPY_DB --key KEY --ratings RATINGS [--out FILE]

Both subcommands also take ``--config PATH`` (default config/settings.yaml).

sheet
    Draws a seeded sample of 40 items in four strata of 10:

    * top     - the first ``pipeline.max_items_per_run`` ENRICHED rows (50)
    * middle  - the first half of the remaining ENRICHED rows
    * bottom  - the second half
    * filtered - FILTERED rows

    ENRICHED rows are ordered by the tie-break ruled for TD-023 (importance_score,
    engagement, topic, published_date, all descending, then id ascending).
    DUPLICATE rows are not sampled. The 40 items are shuffled with the same
    seeded generator and written to DIR as two new files:

    * ``sheet.html`` - a self-contained form (no network, no external files) that
      shows, per item, only a row number, the type (makale / kod deposu / model /
      uygulama), the title and the abstract or description, with any URL inside
      the text replaced by "[link]". No score, source, signal, date, URL or item
      id is in the file. The rater answers G first; K1-K4 appear only after G.
      Ratings are downloaded as ``ratings.csv``.
    * ``key.csv`` - row number to item id, with the seed, the snapshot's
      SHA-256 and the sheet id. Strata and scores are not in it: ``analyze``
      recomputes them from the snapshot.

analyze
    Checks that the snapshot is the one the key was made from and that the key
    is exactly the sample the seed produces, validates the ratings (every row
    once, every value an integer 1-5), and reports:

    * Spearman's rho between G and importance_score (all rows; ENRICHED only),
      with a seeded bootstrap 95% interval;
    * mean G and the share of G >= 4 per stratum, with Wilson 95% intervals;
    * every FILTERED item rated G >= 4;
    * K1-K4 (and G) against the four ranking signals. FILTERED rows store no
      breakdown, so the breakdown of every ranked row is recomputed with the
      application's own Scorer, at the date the rows were ranked, and accepted
      only if it reproduces every stored importance_score and every stored
      ENRICHED breakdown. If it does not, the comparison uses ENRICHED rows only
      and the report says so;
    * K2 against novelty (ENRICHED rows only), K3 against the length of the
      available text;
    * a per-source breakdown;
    * the caveats that follow from 40 items and one rater.

Read-only by construction: ``mode=ro`` URI (plus ``immutable=1`` when no
``-wal`` file is present), ``PRAGMA query_only = ON``, SELECT statements only,
refusal to open the configured live database or anything in its directory,
refusal to write into that directory or over an existing file, and a SHA-256
of the copy taken before and after the run.

Exit status: 0 on success; 1 if the copy changed during the run; 2 on a usage
error, a refused path, or invalid key or ratings.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
import sqlite3
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

SAMPLE_PER_STRATUM: int = 10
STRATA: tuple[str, ...] = ("top", "middle", "bottom", "filtered")
DEFAULT_SEED: int = 20261117
BOOTSTRAP_RESAMPLES: int = 2000
SCORE_TOLERANCE: float = 1e-9
WILSON_Z: float = 1.959963984540054
CRITERIA: tuple[str, ...] = ("G", "K1", "K2", "K3", "K4")
RATINGS_HEADER: list[str] = ["row", *CRITERIA, "note"]
SIGNALS: tuple[str, ...] = ("recency", "authority", "engagement", "topic")
KEY_MAGIC = "# arip-evaluate-relevance key v1"
RATINGS_MAGIC = "# arip-evaluate-relevance ratings v1"

TYPE_LABEL: dict[str, str] = {
    "PAPER": "makale",
    "REPO": "kod deposu",
    "MODEL": "model",
    "SPACE": "uygulama",
}
URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

QUESTIONS: list[dict[str, str]] = [
    {
        "key": "G",
        "text": "ARIP bu konuda içerik üretsin ister miydim?",
        "scale": "1 = kesinlikle hayır, 3 = kararsızım, 5 = kesinlikle evet",
    },
    {
        "key": "K1",
        "text": "Konu uygunluğu — yapay zekâ araştırması veya pratiğiyle ne kadar ilgili?",
        "scale": "1 = alakasız, 5 = tam merkezde",
    },
    {
        "key": "K2",
        "text": "Önem / yenilik — alanda gerçekten yeni veya önemli bir şey mi?",
        "scale": "1 = sıradan, küçük sürüm veya tekrar, 5 = dikkat çekici yeni fikir ya da sonuç",
    },
    {
        "key": "K3",
        "text": "Anlatılabilirlik — buradaki bilgiyle iyi bir eğitici içerik yazılabilir mi?",
        "scale": "1 = neredeyse bilgi yok, 5 = zengin",
    },
    {
        "key": "K4",
        "text": "İlk izlenim — başlığı görünce tıklamak ister miydim?",
        "scale": "1 = hayır, 5 = hemen",
    },
]


class RefusedError(Exception):
    """A path, key or ratings file that makes the run unsafe or invalid; exit status 2."""


# ---------------------------------------------------------------------------
# Read-only access (copied from tools/measure_ranking_pool.py, Stage 2 ruling Q3)
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
    live_db: Path
    top_n: int
    ranking: object


def read_settings(config: Path) -> ToolSettings:
    from sqlalchemy.engine import make_url

    from arip.config import load_settings

    settings = load_settings(config)
    return ToolSettings(
        live_db=Path(make_url(settings.database.url).database or ""),
        top_n=settings.pipeline.max_items_per_run,
        ranking=settings.ranking,
    )


def guard_copy(copy: Path, live: Path) -> None:
    """Refuse anything that is, or sits beside, the configured live database."""
    if not copy.is_file():
        raise RefusedError(f"copy not found: {copy}")
    if copy.resolve() == live.resolve() or _is_within(copy, live.parent):
        raise RefusedError(
            f"refusing to read {copy}: it is the configured database or in its "
            f"directory ({live.parent.resolve()}). Use a snapshot instead."
        )


def guard_new_file(path: Path, live: Path) -> None:
    """Refuse to overwrite a file or to write into the live database's directory."""
    if path.exists():
        raise RefusedError(f"output file already exists: {path}")
    if not path.parent.is_dir():
        raise RefusedError(f"output directory does not exist: {path.parent}")
    if _is_within(path, live.parent):
        raise RefusedError(
            f"refusing to write {path}: it is in the live database's directory "
            f"({live.parent.resolve()})"
        )


# ---------------------------------------------------------------------------
# Pool and sample
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    id: int
    source_id: str
    source_type: str
    status: str
    score: float | None
    breakdown: dict | None
    title: str | None
    abstract: str | None
    published: date | None
    ranked_at: str | None


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


def load_rows(conn: sqlite3.Connection) -> list[Row]:
    rows = []
    cursor = conn.execute(
        "SELECT id, source_id, source_type, status, importance_score, signal_breakdown, "
        "title, abstract, published_date, ranked_at FROM items ORDER BY id"
    )
    for item_id, source_id, source_type, status, score, raw, title, abstract, pub, ranked in cursor:
        try:
            breakdown = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            breakdown = None
        rows.append(
            Row(
                id=item_id,
                source_id=source_id,
                source_type=source_type,
                status=status,
                score=None if score is None else float(score),
                breakdown=breakdown if isinstance(breakdown, dict) else None,
                title=title,
                abstract=abstract,
                published=_as_date(pub),
                ranked_at=None if ranked is None else str(ranked),
            )
        )
    return rows


def order_key(row: Row) -> tuple[float, float, float, float, int]:
    """The TD-023 tie-break: score, engagement, topic, published_date desc; id asc."""
    score = -math.inf if row.score is None else row.score
    engagement = _signal(row.breakdown, "engagement")
    topic = _signal(row.breakdown, "topic")
    published = -math.inf if row.published is None else float(row.published.toordinal())
    return (
        -score,
        -(-math.inf if engagement is None else engagement),
        -(-math.inf if topic is None else topic),
        -published,
        row.id,
    )


def strata(rows: Sequence[Row], top_n: int) -> dict[str, list[int]]:
    """Item ids per stratum. ENRICHED in the ruled order; FILTERED in id order."""
    enriched = [r.id for r in sorted((r for r in rows if r.status == "ENRICHED"), key=order_key)]
    rest = enriched[top_n:]
    half = len(rest) // 2
    return {
        "top": enriched[:top_n],
        "middle": rest[:half],
        "bottom": rest[half:],
        "filtered": sorted(r.id for r in rows if r.status == "FILTERED"),
    }


def draw_sample(groups: dict[str, list[int]], seed: int) -> list[int]:
    """Ten ids per stratum, then all of them shuffled; one seeded generator."""
    rng = random.Random(seed)
    chosen: list[int] = []
    for name in STRATA:
        members = groups[name]
        if len(members) < SAMPLE_PER_STRATUM:
            raise RefusedError(
                f"stratum '{name}' has {len(members)} items; {SAMPLE_PER_STRATUM} are needed"
            )
        chosen.extend(rng.sample(members, SAMPLE_PER_STRATUM))
    rng.shuffle(chosen)
    return chosen


def blind_text(text: str | None) -> str:
    """The item text as shown: whitespace kept, any URL replaced by '[link]'."""
    return URL_PATTERN.sub("[link]", text or "").strip()


def sheet_id(seed: int, snapshot_sha: str, ids: Sequence[int]) -> str:
    payload = f"{seed}|{snapshot_sha}|{','.join(map(str, ids))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def sheet_items(rows_by_id: dict[int, Row], ids: Sequence[int]) -> list[dict[str, object]]:
    """Exactly what the sheet may show: row number, type, title, text."""
    items = []
    for number, item_id in enumerate(ids, start=1):
        row = rows_by_id[item_id]
        items.append(
            {
                "row": number,
                "type": TYPE_LABEL.get(row.source_type, "diğer"),
                "title": blind_text(row.title),
                "text": blind_text(row.abstract),
            }
        )
    return items


# ---------------------------------------------------------------------------
# The HTML form
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ARIP değerlendirme formu</title>
<style>
:root { --ink:#1b1f24; --muted:#5b6470; --line:#d9dee4; --bg:#f6f7f9; --card:#fff; --accent:#1f3864; --on:#1f3864; --onink:#fff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 "Segoe UI", system-ui, sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--accent); color:#fff; padding:10px 16px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
header h1 { font-size:16px; margin:0 12px 0 0; font-weight:600; }
header .progress { font-variant-numeric:tabular-nums; margin-right:auto; }
header button, header label.file { background:#fff; color:var(--accent); border:0; border-radius:4px; padding:6px 12px; font:inherit; cursor:pointer; }
header input[type=file] { display:none; }
main { max-width:860px; margin:0 auto; padding:16px; }
.intro { color:var(--muted); margin:4px 0 16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:14px 16px; margin:0 0 14px; }
.card.done { border-left:5px solid #2e7d32; }
.meta { color:var(--muted); font-size:13px; }
.title { font-weight:600; font-size:16px; margin:2px 0 6px; overflow-wrap:anywhere; }
.text { white-space:pre-wrap; overflow-wrap:anywhere; margin:0 0 6px; }
.text.empty { color:var(--muted); font-style:italic; }
.toggle { background:none; border:0; color:var(--accent); padding:0; font:inherit; cursor:pointer; text-decoration:underline; }
.q { margin:10px 0 0; }
.q .label { font-weight:600; }
.q .scale { color:var(--muted); font-size:13px; }
.buttons { display:flex; gap:6px; margin-top:4px; }
.buttons button { width:40px; height:34px; border:1px solid var(--line); border-radius:4px; background:#fff; font:inherit; cursor:pointer; }
.buttons button.on { background:var(--on); color:var(--onink); border-color:var(--on); }
.ks[hidden] { display:none; }
textarea { width:100%; min-height:38px; margin-top:8px; font:inherit; padding:6px; border:1px solid var(--line); border-radius:4px; }
.status { font-size:13px; color:var(--muted); margin-left:8px; }
</style>
</head>
<body>
<header>
  <h1>ARIP değerlendirme</h1>
  <span class="progress" id="progress"></span>
  <button type="button" id="download">Puanları indir</button>
  <label class="file">Puan dosyası yükle<input type="file" id="load" accept=".csv,text/csv"></label>
  <span class="status" id="status"></span>
</header>
<main>
  <p class="intro">Her öğe için önce genel soruyu (G) yanıtlayın; diğer sorular ondan sonra açılır. Puanlar bu tarayıcıda da saklanır, ama asıl kayıt "Puanları indir" ile alınan dosyadır. Sayfa kimliği: <span id="sheet-id"></span></p>
  <div id="cards"></div>
</main>
<script type="application/json" id="sheet-data">__DATA__</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("sheet-data").textContent);
  var KEYS = ["G", "K1", "K2", "K3", "K4"];
  var PREVIEW = 600;
  var STORE = "arip-eval-" + DATA.sheet;
  var state = {};
  DATA.items.forEach(function (it) { state[it.row] = { G: 0, K1: 0, K2: 0, K3: 0, K4: 0, note: "" }; });

  function save() {
    try { window.localStorage.setItem(STORE, JSON.stringify(state)); } catch (e) { /* storage may be unavailable */ }
  }
  function restore() {
    try {
      var raw = window.localStorage.getItem(STORE);
      if (!raw) { return; }
      var saved = JSON.parse(raw);
      Object.keys(saved).forEach(function (row) { if (state[row]) { state[row] = saved[row]; } });
    } catch (e) { /* ignore unreadable storage */ }
  }
  function complete(s) { return KEYS.every(function (k) { return s[k] >= 1 && s[k] <= 5; }); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function question(card, it, q) {
    var box = el("div", "q");
    box.appendChild(el("div", "label", q.key + ". " + q.text));
    box.appendChild(el("div", "scale", q.scale));
    var buttons = el("div", "buttons");
    for (var v = 1; v <= 5; v++) {
      var b = el("button", "", String(v));
      b.type = "button";
      b.setAttribute("data-row", it.row);
      b.setAttribute("data-key", q.key);
      b.setAttribute("data-value", v);
      b.addEventListener("click", onRate);
      buttons.appendChild(b);
    }
    box.appendChild(buttons);
    return box;
  }

  function build() {
    var root = document.getElementById("cards");
    DATA.items.forEach(function (it) {
      var card = el("section", "card");
      card.id = "row-" + it.row;
      card.appendChild(el("div", "meta", "Satır " + it.row + " · " + it.type));
      card.appendChild(el("div", "title", it.title || "(başlık yok)"));
      var full = it.text || "";
      var text = el("p", full ? "text" : "text empty", full ? full.slice(0, PREVIEW) : "(açıklama yok)");
      card.appendChild(text);
      if (full.length > PREVIEW) {
        text.textContent = full.slice(0, PREVIEW) + "…";
        var t = el("button", "toggle", "Tamamını göster");
        t.type = "button";
        t.addEventListener("click", function () {
          var open = t.getAttribute("data-open") === "1";
          text.textContent = open ? full.slice(0, PREVIEW) + "…" : full;
          t.textContent = open ? "Tamamını göster" : "Kısalt";
          t.setAttribute("data-open", open ? "0" : "1");
        });
        card.appendChild(t);
      }
      card.appendChild(question(card, it, DATA.questions[0]));
      var ks = el("div", "ks");
      ks.hidden = true;
      ks.id = "ks-" + it.row;
      for (var i = 1; i < DATA.questions.length; i++) { ks.appendChild(question(card, it, DATA.questions[i])); }
      var note = el("textarea");
      note.placeholder = "Not (isteğe bağlı)";
      note.setAttribute("data-row", it.row);
      note.addEventListener("input", function (e) { state[it.row].note = e.target.value; save(); });
      ks.appendChild(note);
      card.appendChild(ks);
      root.appendChild(card);
    });
  }

  function render() {
    var done = 0;
    DATA.items.forEach(function (it) {
      var s = state[it.row];
      var card = document.getElementById("row-" + it.row);
      card.querySelectorAll(".buttons button").forEach(function (b) {
        var on = s[b.getAttribute("data-key")] === Number(b.getAttribute("data-value"));
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      document.getElementById("ks-" + it.row).hidden = !(s.G >= 1);
      card.querySelector("textarea").value = s.note || "";
      var ok = complete(s);
      card.classList.toggle("done", ok);
      if (ok) { done++; }
    });
    var total = DATA.items.length;
    document.getElementById("progress").textContent =
      "Tamamlanan: " + done + " / " + total + (done === total ? " — hepsi puanlandı" : "");
  }

  function onRate(e) {
    var b = e.currentTarget;
    var row = Number(b.getAttribute("data-row"));
    var key = b.getAttribute("data-key");
    if (key !== "G" && !(state[row].G >= 1)) { return; }
    state[row][key] = Number(b.getAttribute("data-value"));
    save();
    render();
  }

  function csvField(value) { return '"' + String(value).replace(/[\\r\\n]+/g, " ").replace(/"/g, '""') + '"'; }

  function toCsv() {
    var lines = ["# arip-evaluate-relevance ratings v1", "# sheet: " + DATA.sheet, "row,G,K1,K2,K3,K4,note"];
    DATA.items.forEach(function (it) {
      var s = state[it.row];
      var cells = [it.row];
      KEYS.forEach(function (k) { cells.push(s[k] >= 1 ? s[k] : ""); });
      cells.push(csvField(s.note || ""));
      lines.push(cells.join(","));
    });
    return lines.join("\\r\\n") + "\\r\\n";
  }

  function parseCsvLine(line) {
    var out = [], cur = "", quoted = false;
    for (var i = 0; i < line.length; i++) {
      var c = line[i];
      if (quoted) {
        if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; }
        else if (c === '"') { quoted = false; }
        else { cur += c; }
      } else if (c === '"') { quoted = true; }
      else if (c === ",") { out.push(cur); cur = ""; }
      else { cur += c; }
    }
    out.push(cur);
    return out;
  }

  function load(text) {
    var lines = text.replace(/^\\ufeff/, "").split(/\\r?\\n/).filter(function (l) { return l.length; });
    var sheetLine = lines.filter(function (l) { return l.indexOf("# sheet: ") === 0; })[0];
    if (!sheetLine || sheetLine.slice(9).trim() !== DATA.sheet) {
      document.getElementById("status").textContent = "Bu dosya başka bir sayfaya ait; yüklenmedi.";
      return;
    }
    var n = 0;
    lines.forEach(function (l) {
      if (l[0] === "#" || l.indexOf("row,") === 0) { return; }
      var cells = parseCsvLine(l);
      var row = Number(cells[0]);
      if (!state[row]) { return; }
      KEYS.forEach(function (k, i) {
        var v = Number(cells[i + 1]);
        state[row][k] = v >= 1 && v <= 5 ? v : 0;
      });
      state[row].note = cells[6] || "";
      n++;
    });
    save();
    render();
    document.getElementById("status").textContent = n + " satır yüklendi.";
  }

  document.getElementById("download").addEventListener("click", function () {
    var blob = new Blob(["\\ufeff" + toCsv()], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "ratings.csv";
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    document.getElementById("status").textContent = "ratings.csv indirildi.";
  });
  document.getElementById("load").addEventListener("change", function (e) {
    var file = e.target.files[0];
    if (!file) { return; }
    var reader = new FileReader();
    reader.onload = function () { load(String(reader.result)); };
    reader.readAsText(file, "utf-8");
    e.target.value = "";
  });

  document.getElementById("sheet-id").textContent = DATA.sheet;
  build();
  restore();
  render();
})();
</script>
</body>
</html>
"""


def render_sheet(sid: str, items: list[dict[str, object]]) -> str:
    data = json.dumps({"sheet": sid, "questions": QUESTIONS, "items": items}, ensure_ascii=False)
    # Inside a <script> element only "</" can end it early; "<\\/" is the same JSON string.
    data = data.replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__DATA__", data)


def write_key(path: Path, seed: int, snapshot_sha: str, sid: str, ids: Sequence[int]) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(f"{KEY_MAGIC}\r\n# seed: {seed}\r\n# snapshot_sha256: {snapshot_sha}\r\n")
        handle.write(f"# sheet: {sid}\r\n")
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(["row", "item_id"])
        for number, item_id in enumerate(ids, start=1):
            writer.writerow([number, item_id])


# ---------------------------------------------------------------------------
# Key and ratings
# ---------------------------------------------------------------------------


@dataclass
class Key:
    seed: int
    snapshot_sha: str
    sheet: str
    ids: list[int]


def _meta_and_body(path: Path, magic: str) -> tuple[dict[str, str], list[str]]:
    if not path.is_file():
        raise RefusedError(f"file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0].strip() != magic:
        raise RefusedError(f"{path} does not start with '{magic}'")
    meta: dict[str, str] = {}
    body: list[str] = []
    for line in lines[1:]:
        if line.startswith("#"):
            name, _, value = line[1:].partition(":")
            meta[name.strip()] = value.strip()
        else:
            body.append(line)
    return meta, body


def read_key(path: Path) -> Key:
    meta, body = _meta_and_body(path, KEY_MAGIC)
    try:
        seed = int(meta["seed"])
        snapshot_sha, sid = meta["snapshot_sha256"], meta["sheet"]
    except (KeyError, ValueError) as exc:
        raise RefusedError(f"{path}: missing or invalid header line ({exc})") from exc
    reader = list(csv.reader(body))
    if not reader or reader[0] != ["row", "item_id"]:
        raise RefusedError(f"{path}: the column header must be 'row,item_id'")
    ids = []
    for expected, cells in enumerate(reader[1:], start=1):
        if len(cells) != 2 or cells[0] != str(expected) or not cells[1].isdigit():
            raise RefusedError(f"{path}: line for row {expected} is invalid: {cells}")
        ids.append(int(cells[1]))
    return Key(seed, snapshot_sha, sid, ids)


@dataclass(frozen=True)
class Rating:
    row: int
    values: dict[str, int]
    note: str


def read_ratings(path: Path, key: Key) -> dict[int, Rating]:
    """Every row exactly once, every criterion an integer 1-5, for this sheet."""
    meta, body = _meta_and_body(path, RATINGS_MAGIC)
    if meta.get("sheet") != key.sheet:
        raise RefusedError(
            f"{path} belongs to sheet {meta.get('sheet')!r}, the key to sheet {key.sheet!r}"
        )
    reader = list(csv.reader(body))
    if not reader or reader[0] != RATINGS_HEADER:
        raise RefusedError(f"{path}: the column header must be '{','.join(RATINGS_HEADER)}'")
    problems: list[str] = []
    ratings: dict[int, Rating] = {}
    total = len(key.ids)
    for cells in reader[1:]:
        if len(cells) != len(RATINGS_HEADER):
            problems.append(f"line {cells}: expected {len(RATINGS_HEADER)} fields")
            continue
        if not cells[0].isdigit() or not 1 <= int(cells[0]) <= total:
            problems.append(f"row {cells[0]!r}: not a row of this sheet (1-{total})")
            continue
        row = int(cells[0])
        if row in ratings:
            problems.append(f"row {row}: appears more than once")
            continue
        values: dict[str, int] = {}
        for name, cell in zip(CRITERIA, cells[1:6], strict=True):
            if cell.strip() not in {"1", "2", "3", "4", "5"}:
                problems.append(f"row {row}: {name} is {cell!r}, not an integer 1-5")
            else:
                values[name] = int(cell)
        if len(values) == len(CRITERIA):
            ratings[row] = Rating(row, values, cells[6])
    missing = [r for r in range(1, total + 1) if r not in ratings]
    if missing and not problems:
        problems.append(f"rows without a complete rating: {missing}")
    if problems:
        raise RefusedError(f"{path}: " + "; ".join(problems[:20]))
    return ratings


# ---------------------------------------------------------------------------
# Statistics (pure Python)
# ---------------------------------------------------------------------------


def average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n < 2:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / math.sqrt(sxx * syy)


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman's rho with tie-averaged ranks; None if either side is constant."""
    return pearson(average_ranks(x), average_ranks(y))


def bootstrap_interval(
    x: Sequence[float], y: Sequence[float], seed: int
) -> tuple[float, float] | None:
    """Percentile 95% interval of rho over seeded resamples; None if too few succeed."""
    rng = random.Random(seed)
    n = len(x)
    values = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        picks = [rng.randrange(n) for _ in range(n)]
        rho = spearman([x[i] for i in picks], [y[i] for i in picks])
        if rho is not None:
            values.append(rho)
    if len(values) < BOOTSTRAP_RESAMPLES // 2:
        return None
    values.sort()
    return (values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1])


def wilson(successes: int, n: int) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = successes / n
    z2 = WILSON_Z * WILSON_Z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = WILSON_Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


# ---------------------------------------------------------------------------
# Recomputed breakdowns
# ---------------------------------------------------------------------------


@dataclass
class Recomputed:
    ok: bool
    reason: str
    breakdowns: dict[int, dict[str, float]] = field(default_factory=dict)


def recompute_breakdowns(conn: sqlite3.Connection, ranking: object, runs: int) -> Recomputed:
    """Re-score every ranked row with the application's Scorer and check it.

    The batch is every row with a stored importance_score; the reference date is
    the date of the stored ``ranked_at`` values. The result is accepted only if
    it reproduces every stored importance_score and every stored ENRICHED
    breakdown; otherwise the reason is returned and nothing is used.
    """
    from arip.db.models import Item
    from arip.ranking.scorer import Scorer

    if runs != 1:
        return Recomputed(False, f"{runs} pipeline runs; one ranking batch cannot be assumed")
    cursor = conn.execute(
        "SELECT id, source_id, source_type, status, title, abstract, topics, source_signals, "
        "published_date, importance_score, signal_breakdown, ranked_at FROM items "
        "WHERE importance_score IS NOT NULL ORDER BY id"
    )
    records = cursor.fetchall()
    if not records:
        return Recomputed(False, "no ranked rows")
    dates = {str(r[11])[:10] for r in records if r[11] is not None}
    if len(dates) != 1:
        return Recomputed(False, f"ranked_at spans {len(dates)} dates: {sorted(dates)[:5]}")
    reference = datetime.combine(date.fromisoformat(dates.pop()), datetime.min.time())
    items = [
        Item(
            id=r[0],
            source_id=r[1],
            source_type=r[2],
            status=r[3],
            title=r[4],
            abstract=r[5],
            topics=r[6],
            source_signals=r[7],
            published_date=_as_date(r[8]),
        )
        for r in records
    ]
    results = Scorer(ranking).score_batch(items, now=reference)
    breakdowns: dict[int, dict[str, float]] = {}
    for record, (score, breakdown) in zip(records, results, strict=True):
        item_id, status, stored_score, stored_raw = record[0], record[3], record[9], record[10]
        if abs(score - float(stored_score)) > SCORE_TOLERANCE:
            return Recomputed(
                False,
                f"item {item_id}: recomputed score {score:.10f} != stored {float(stored_score):.10f}",
            )
        if status == "ENRICHED":
            try:
                stored = json.loads(stored_raw) if stored_raw else {}
            except (TypeError, ValueError):
                stored = {}
            for name in SIGNALS:
                value = _signal(stored, name)
                if value is None or abs(value - breakdown[name]) > SCORE_TOLERANCE:
                    return Recomputed(
                        False, f"item {item_id}: recomputed {name} differs from the stored value"
                    )
        breakdowns[item_id] = {name: float(breakdown[name]) for name in SIGNALS}
    return Recomputed(True, f"reproduces all {len(records)} stored scores", breakdowns)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _quiet_logging() -> None:
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))


def _fmt(value: float | None, digits: int = 3) -> str:
    return "   n/a" if value is None else f"{value:+.{digits}f}"


def _interval(bounds: tuple[float, float] | None, pct: bool = False) -> str:
    if bounds is None:
        return "n/a"
    if pct:
        return f"{100 * bounds[0]:.0f}%–{100 * bounds[1]:.0f}%"
    return f"{bounds[0]:+.2f} … {bounds[1]:+.2f}"


def _short(text: str | None, width: int = 70) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def analyse(
    rows: list[Row], key: Key, ratings: dict[int, Rating], top_n: int, recomputed: Recomputed
) -> str:
    by_id = {r.id: r for r in rows}
    groups = strata(rows, top_n)
    stratum_of = {item_id: name for name, ids in groups.items() for item_id in ids}
    sample = [
        (number, by_id[item_id], ratings[number])
        for number, item_id in enumerate(key.ids, start=1)
    ]
    out: list[str] = []
    add = out.append

    add("")
    add("HUMAN-RELEVANCE EVALUATION (TD-025) — one rater, measured, nothing is decided here")
    add(f"  Sheet {key.sheet}; seed {key.seed}; {len(sample)} items, {SAMPLE_PER_STRATUM} per stratum")
    sizes = ", ".join(f"{name} {len(groups[name])}" for name in STRATA)
    add(f"  Stratum sizes in the pool: {sizes} (top = first {top_n} ENRICHED, ruled order)")

    g = [float(r.values["G"]) for _, _, r in sample]
    s = [row.score for _, row, _ in sample]
    add("")
    add("1. G against importance_score (Spearman, tie-averaged ranks; bootstrap 95% interval)")
    rho_all = spearman(g, s)
    add(f"     all {len(g)} items:      rho {_fmt(rho_all)}  [{_interval(bootstrap_interval(g, s, key.seed))}]")
    enr = [(float(r.values["G"]), row.score) for _, row, r in sample if row.status == "ENRICHED"]
    ge, se = [a for a, _ in enr], [b for _, b in enr]
    add(
        f"     ENRICHED {len(ge)} only:   rho {_fmt(spearman(ge, se))}  "
        f"[{_interval(bootstrap_interval(ge, se, key.seed))}]"
    )
    add("     The sample is stratified (10 per stratum), so rho describes these items, not the pool.")

    add("")
    add("2. G per stratum")
    add(f"     {'stratum':<9} {'n':>3} {'mean G':>7} {'G>=4':>6}  Wilson 95%")
    for name in STRATA:
        values = [r.values["G"] for _, row, r in sample if stratum_of.get(row.id) == name]
        high = sum(v >= 4 for v in values)
        mean = statistics.mean(values) if values else float("nan")
        add(
            f"     {name:<9} {len(values):>3} {mean:>7.2f} {high:>3}/{len(values):<2}  "
            f"{_interval(wilson(high, len(values)), pct=True)}"
        )

    top = [r.values["G"] for _, row, r in sample if stratum_of.get(row.id) == "top"]
    top_high = sum(v >= 4 for v in top)
    add("")
    add(
        f"3. Top {top_n}: {top_high} of {len(top)} sampled items rated G >= 4 "
        f"(Wilson 95% {_interval(wilson(top_high, len(top)), pct=True)})"
    )

    add("")
    add("4. FILTERED items rated G >= 4 (the ranking dropped them)")
    filtered_high = [
        (number, row, r) for number, row, r in sample if row.status == "FILTERED" and r.values["G"] >= 4
    ]
    if not filtered_high:
        add("     none")
    for number, row, r in filtered_high:
        add(
            f"     row {number:>2}  G {r.values['G']}  score {row.score:.4f}  "
            f"{row.source_id:<20} {_short(row.title)}"
        )

    add("")
    add("5. Ranking signals for the comparison")
    if recomputed.ok:
        add(f"     recomputed with the application's Scorer: {recomputed.reason}")
        add("     K1–K4 are compared with the signals on all sampled items.")
        signals = {row.id: recomputed.breakdowns[row.id] for _, row, _ in sample}
        compared = sample
    else:
        add(f"     RECOMPUTATION REJECTED: {recomputed.reason}")
        add("     FILTERED rows store no breakdown, so K1–K4 are compared on ENRICHED rows only,")
        add("     with their stored breakdowns.")
        compared = [t for t in sample if t[1].status == "ENRICHED"]
        signals = {
            row.id: {name: _signal(row.breakdown, name) or 0.0 for name in SIGNALS}
            for _, row, _ in compared
        }

    add("")
    add(f"6. Spearman rho, criteria against signals (n = {len(compared)})")
    add("     " + f"{'':<4}" + "".join(f"{name:>12}" for name in SIGNALS))
    for crit in CRITERIA:
        cells = []
        for name in SIGNALS:
            x = [float(r.values[crit]) for _, _, r in compared]
            y = [signals[row.id][name] for _, row, _ in compared]
            cells.append(f"{_fmt(spearman(x, y)):>12}")
        add(f"     {crit:<4}" + "".join(cells))
    distinct = "  ".join(
        f"{name} {len({round(signals[row.id][name], 12) for _, row, _ in compared})}"
        for name in SIGNALS
    )
    add(f"     distinct signal values: {distinct}")
    add("     Pairs that carry a meaning: K1–topic, K2–engagement. Authority is constant per")
    add("     source, and engagement is one declared value for every arxiv item, so those")
    add("     columns are heavily tied. K3 and K4 have no stored counterpart.")

    add("")
    add("7. Criteria without a stored signal")
    nov = [
        (float(r.values["K2"]), _signal(row.breakdown, "novelty"))
        for _, row, r in sample
        if row.status == "ENRICHED" and _signal(row.breakdown, "novelty") is not None
    ]
    add(
        f"     K2 against novelty (ENRICHED only, n = {len(nov)}): "
        f"rho {_fmt(spearman([a for a, _ in nov], [b for _, b in nov]))}"
    )
    k3 = [float(r.values["K3"]) for _, _, r in sample]
    lengths = [float(len(blind_text(row.abstract))) for _, row, _ in sample]
    add(f"     K3 against the length of the shown text (n = {len(k3)}): rho {_fmt(spearman(k3, lengths))}")
    empty = [r.values["K3"] for _, row, r in sample if not blind_text(row.abstract)]
    if empty:
        add(f"     items shown without any text: {len(empty)}, mean K3 {statistics.mean(empty):.2f}")

    add("")
    add("8. Per source")
    add(f"     {'source':<20} {'n':>3} {'mean G':>7} {'G>=4':>6}  Wilson 95%")
    counts = Counter(row.source_id for _, row, _ in sample)
    for source in sorted(counts):
        values = [r.values["G"] for _, row, r in sample if row.source_id == source]
        high = sum(v >= 4 for v in values)
        bounds = _interval(wilson(high, len(values)), pct=True) if len(values) >= 5 else "(n < 5)"
        add(
            f"     {source:<20} {len(values):>3} {statistics.mean(values):>7.2f} "
            f"{high:>3}/{len(values):<2}  {bounds}"
        )

    add("")
    add("9. Caveats")
    add("     - One rater, 40 items. With n = 40 the 95% interval of a Spearman rho is about")
    add("       +-0.3 wide on each side; with 10 per stratum, a share's interval spans tens of")
    add("       percentage points (section 2). These are indications, not estimates.")
    add("     - The sample is stratified, not proportional; pool-wide figures cannot be read")
    add("       off it without weighting.")
    add("     - Ratings were blind to score, source, dates and URLs; the rater still saw the")
    add("       item type and text, which are also inputs to the ranking.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_sheet(copy: Path, config: Path, out_dir: Path, seed: int) -> int:
    settings = read_settings(config)
    guard_copy(copy, settings.live_db)
    sheet_path, key_path = out_dir / "sheet.html", out_dir / "key.csv"
    guard_new_file(sheet_path, settings.live_db)
    guard_new_file(key_path, settings.live_db)
    before = sha256_of(copy)
    conn = connect_read_only(copy)
    try:
        rows = load_rows(conn)
    finally:
        conn.close()
    after = sha256_of(copy)
    if after != before:
        print(f"Copy changed during the run: {before} -> {after}", file=sys.stderr)
        return 1
    groups = strata(rows, settings.top_n)
    ids = draw_sample(groups, seed)
    sid = sheet_id(seed, before, ids)
    html = render_sheet(sid, sheet_items({r.id: r for r in rows}, ids))
    with sheet_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(html)
    write_key(key_path, seed, before, sid, ids)
    print(f"Copy: {copy.resolve()}")
    print(f"SHA-256: {before} (unchanged)")
    print("Stratum sizes: " + ", ".join(f"{name} {len(groups[name])}" for name in STRATA))
    print(f"Sample: {len(ids)} items, seed {seed}, sheet {sid}")
    print(f"Written: {sheet_path.resolve()}")
    print(f"Written: {key_path.resolve()}  (do not open before rating)")
    return 0


def run_analyze(copy: Path, config: Path, key_path: Path, ratings_path: Path, out: Path | None) -> int:
    settings = read_settings(config)
    guard_copy(copy, settings.live_db)
    if out is not None:
        guard_new_file(out, settings.live_db)
    key = read_key(key_path)
    before = sha256_of(copy)
    if before != key.snapshot_sha:
        raise RefusedError(
            f"the copy's SHA-256 {before} is not the snapshot the key was made from "
            f"({key.snapshot_sha})"
        )
    ratings = read_ratings(ratings_path, key)
    _quiet_logging()
    conn = connect_read_only(copy)
    try:
        rows = load_rows(conn)
        runs = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
        expected = draw_sample(strata(rows, settings.top_n), key.seed)
        if expected != key.ids:
            raise RefusedError("the key is not the sample this seed draws from this snapshot")
        recomputed = recompute_breakdowns(conn, settings.ranking, runs)
    finally:
        conn.close()
    after = sha256_of(copy)
    text = analyse(rows, key, ratings, settings.top_n, recomputed)
    unchanged = after == before
    report = (
        f"Copy: {copy.resolve()}\nSHA-256 before: {before}\n{text}\n\n"
        f"SHA-256 after:  {after}  ({'unchanged' if unchanged else 'CHANGED'})"
    )
    print(report)
    if out is not None:
        with out.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(report + "\n")
    return 0 if unchanged else 1


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="evaluate_relevance", description="Blind human-relevance evaluation (TD-025)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=Path("config/settings.yaml"))
    sheet = sub.add_parser("sheet", parents=[common], help="draw the sample and write the form")
    sheet.add_argument("copy", type=Path)
    sheet.add_argument("--out-dir", type=Path, required=True)
    sheet.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ana = sub.add_parser("analyze", parents=[common], help="analyse the ratings")
    ana.add_argument("copy", type=Path)
    ana.add_argument("--key", type=Path, required=True)
    ana.add_argument("--ratings", type=Path, required=True)
    ana.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "sheet":
            return run_sheet(args.copy, args.config, args.out_dir, args.seed)
        return run_analyze(args.copy, args.config, args.key, args.ratings, args.out)
    except RefusedError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
