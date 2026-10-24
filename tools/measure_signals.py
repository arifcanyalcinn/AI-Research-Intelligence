import json, sqlite3, sys
from pathlib import Path
from arip.config import load_settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = load_settings(Path("config/settings.yaml"))
W = s.ranking.weights
db = Path(s.database.url.split("///", 1)[-1])
con = sqlite3.connect(str(db)); con.row_factory = sqlite3.Row

rows = [r for r in con.execute(
    "SELECT id, source_id, importance_score, title, signal_breakdown FROM items "
    "WHERE signal_breakdown IS NOT NULL AND status IN ('ENRICHED','DUPLICATE','FILTERED')")]

KEYS = ("recency", "authority", "engagement", "topic")
WT = {"recency": W.recency, "authority": W.authority,
      "engagement": W.engagement, "topic": W.topic}

by = {}
for r in rows:
    b = json.loads(r["signal_breakdown"])
    by.setdefault(r["source_id"], []).append(b)

print(f"\nAgirliklar: recency {W.recency}  authority {W.authority}  "
      f"engagement {W.engagement}  topic {W.topic}\n")
print(f"{'KAYNAK':<22}{'N':>5}  " + "".join(f"{k[:3].upper():>8}" for k in KEYS)
      + "   " + "".join(f"{'%0-'+k[:3]:>9}" for k in KEYS))
print("-" * 100)
for src, bs in sorted(by.items()):
    means = {k: sum(b.get(k, 0.0) for b in bs) / len(bs) for k in KEYS}
    zeros = {k: 100.0 * sum(1 for b in bs if b.get(k, 0.0) == 0.0) / len(bs) for k in KEYS}
    print(f"{src:<22}{len(bs):>5}  " + "".join(f"{means[k]:>8.3f}" for k in KEYS)
          + "   " + "".join(f"{zeros[k]:>8.0f}%" for k in KEYS))

print("\n\nTD-016 — metriksiz kaynak yeniden normalize edilseydi:\n")
print(f"{'KAYNAK':<22}{'SIMDI':>8}{'YENIDEN':>10}{'FARK':>8}   aciklama")
print("-" * 80)
for src, bs in sorted(by.items()):
    now = sum(sum(b.get(k, 0.0) * WT[k] for k in KEYS) for b in bs) / len(bs)
    dead = [k for k in KEYS if all(b.get(k, 0.0) == 0.0 for b in bs)]
    live = [k for k in KEYS if k not in dead]
    tot = sum(WT[k] for k in live) or 1.0
    renorm = sum(sum(b.get(k, 0.0) * WT[k] / tot for k in live) for b in bs) / len(bs)
    note = ("her ogede sifir: " + ", ".join(dead)) if dead else "tum sinyaller canli"
    print(f"{src:<22}{now:>8.3f}{renorm:>10.3f}{renorm-now:>+8.3f}   {note}")

thr = s.ranking.min_score
print(f"\n\nIlk 50'de kaynak dagilimi (min_score={thr}):\n")
scored = sorted(((sum(json.loads(r['signal_breakdown']).get(k,0.0)*WT[k] for k in KEYS),
                  r["source_id"]) for r in rows), reverse=True)
cur, ren = {}, {}
for _, src in scored[:50]:
    cur[src] = cur.get(src, 0) + 1
rescored = []
for r in rows:
    b = json.loads(r["signal_breakdown"]); src = r["source_id"]
    dead = [k for k in KEYS if all(x.get(k, 0.0) == 0.0 for x in by[src])]
    live = [k for k in KEYS if k not in dead]
    tot = sum(WT[k] for k in live) or 1.0
    rescored.append((sum(b.get(k, 0.0) * WT[k] / tot for k in live), src))
for _, src in sorted(rescored, reverse=True)[:50]:
    ren[src] = ren.get(src, 0) + 1
print(f"{'KAYNAK':<22}{'SIMDI':>8}{'YENIDEN':>10}")
for src in sorted(set(cur) | set(ren)):
    print(f"{src:<22}{cur.get(src,0):>8}{ren.get(src,0):>10}")