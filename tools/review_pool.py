import csv, json, sqlite3, sys
from pathlib import Path
from arip.config import load_settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

db = Path(load_settings(Path("config/settings.yaml")).database.url.split("///", 1)[-1])
con = sqlite3.connect(str(db))
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT id, source_id, importance_score, title, primary_url,
           published_date, signal_breakdown
    FROM items WHERE status = 'ENRICHED'
    ORDER BY importance_score DESC
""").fetchall()

def sig(r):
    b = json.loads(r["signal_breakdown"] or "{}")
    return [round(b.get(k, 0.0), 3) for k in
            ("recency", "authority", "engagement", "topic", "novelty")]

print(f"\n{len(rows)} ENRICHED item, puana gore siralandi. Ilk 40:\n")
print(f"{'#':>3}  {'PUAN':>5}  {'KAYNAK':<20}  {'REC':>5} {'ENG':>5} {'TOP':>5}  BASLIK")
print("-" * 118)
for i, r in enumerate(rows[:40], 1):
    rec, auth, eng, top, nov = sig(r)
    t = (r["title"] or "")[:60]
    print(f"{i:>3}  {r['importance_score']:>5.3f}  {r['source_id']:<20}  "
          f"{rec:>5.2f} {eng:>5.2f} {top:>5.2f}  {t}")

print("\nKaynak dagilimi (ilk 50):")
top50 = {}
for r in rows[:50]:
    top50[r["source_id"]] = top50.get(r["source_id"], 0) + 1
for k, v in sorted(top50.items(), key=lambda x: -x[1]):
    print(f"   {k:<22} {v}")

import html

def esc(s):
    return html.escape(str(s or ""))

parts = ["""<!doctype html><meta charset="utf-8"><title>ENRICHED havuzu</title>
<style>
body{font:14px system-ui;margin:24px;background:#fafafa}
table{border-collapse:collapse;width:100%;background:#fff}
th{position:sticky;top:0;background:#1f3864;color:#fff;padding:8px;text-align:left;font-size:12px}
td{padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}
tr:hover{background:#f2f7ff}
a{color:#1f3864}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.zero{color:#c00;font-weight:600}
.src{font-size:12px;color:#555;white-space:nowrap}
</style>
<table><tr><th>#</th><th>Puan</th><th>Kaynak</th><th>Rec</th><th>Eng</th><th>Top</th><th>Baslik</th><th>Tarih</th></tr>"""]

for i, r in enumerate(rows, 1):
    rec, auth, eng, top, nov = sig(r)
    z = ' class="num zero"' if top == 0 else ' class="num"'
    parts.append(
        f'<tr><td class="num">{i}</td>'
        f'<td class="num">{r["importance_score"]:.3f}</td>'
        f'<td class="src">{esc(r["source_id"])}</td>'
        f'<td class="num">{rec:.2f}</td><td class="num">{eng:.2f}</td><td{z}>{top:.2f}</td>'
        f'<td><a href="{esc(r["primary_url"])}" target="_blank">{esc(r["title"])}</a></td>'
        f'<td class="src">{esc(r["published_date"])}</td></tr>')

parts.append("</table>")
Path("enriched_pool.html").write_text("".join(parts), encoding="utf-8")
print(f"\nenriched_pool.html yazildi ({len(rows)} satir) — cift tiklayip tarayicida ac.")

print(f"\nTamami enriched_pool.csv dosyasina yazildi ({len(rows)} satir).")