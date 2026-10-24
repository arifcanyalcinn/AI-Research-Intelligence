import json, random, sqlite3, sys, html
from pathlib import Path
from arip.config import load_settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = load_settings(Path("config/settings.yaml"))
W = {"recency": s.ranking.weights.recency, "authority": s.ranking.weights.authority,
     "engagement": s.ranking.weights.engagement, "topic": s.ranking.weights.topic}
con = sqlite3.connect(str(Path(s.database.url.split("///", 1)[-1])))
con.row_factory = sqlite3.Row

items = []
for r in con.execute("SELECT id, source_id, title, abstract, primary_url, published_date, "
                     "signal_breakdown, status FROM items WHERE signal_breakdown IS NOT NULL "
                     "AND status = 'ENRICHED'"):
    b = json.loads(r["signal_breakdown"])
    g = lambda k: b.get(k, 0.0)
    items.append({"id": r["id"], "src": r["source_id"], "title": r["title"] or "",
                  "abs": (r["abstract"] or "")[:320], "url": r["primary_url"] or "",
                  "date": r["published_date"] or "", "rec": g("recency"),
                  "eng": g("engagement"), "top": g("topic"),
                  "score": sum(g(k) * W[k] for k in W)})
items.sort(key=lambda x: -x["score"])
for i, it in enumerate(items, 1):
    it["rank"] = i

papers = [i for i in items if i["src"] == "huggingface_papers"]
blocks = []

a = papers[:4] + papers[-4:]
random.Random(7).shuffle(a)
blocks.append(("A", "Kor karsilastirma — puan ve kaynak gizli. Sadece oku ve puanla.", a, True))

hi = [i for i in items if i["eng"] >= 0.80 and i["top"] == 0.0][:3]
lo = [i for i in items if i["top"] >= 0.67 and i["eng"] <= 0.40][:3]
blocks.append(("B", "Yuksek ilgi / sifir konu  vs  dusuk ilgi / yuksek konu", hi + lo, False))

ax = sorted([i for i in items if i["src"] == "arxiv"],
            key=lambda x: -(0.25*x["rec"] + 0.25*0.85 + 0.35*0.5 + 0.15*x["top"]))[:5]
blocks.append(("C", "ArXiv — su an ilk 50'de hic yok. Gercekten iyi mi?", ax, False))

gh = [i for i in items if i["src"] == "github_trending" and i["top"] == 0.0][:4]
blocks.append(("D", "GitHub, konu eslesmesi sifir. Yapay zeka arastirmasi mi, gurultu mu?", gh, False))

ms = [i for i in items if i["src"] in ("huggingface_models", "huggingface_spaces")][:3]
blocks.append(("E", "HF Models / Spaces — digest'te yeri var mi?", ms, False))

e = lambda t: html.escape(str(t or ""))
out = ["""<!doctype html><meta charset="utf-8"><title>Okuma listesi</title><style>
body{font:15px/1.55 system-ui;margin:0 auto;max-width:860px;padding:28px;background:#fafafa;color:#222}
h2{background:#1f3864;color:#fff;padding:10px 14px;border-radius:5px;font-size:15px;margin:34px 0 6px}
h2 span{font-weight:400;opacity:.85}
.it{background:#fff;border:1px solid #e3e3e3;border-radius:6px;padding:14px 16px;margin:10px 0}
.tag{display:inline-block;background:#1f3864;color:#fff;font:600 12px system-ui;
     padding:2px 8px;border-radius:3px;margin-right:8px}
.t{font-weight:600}
.m{color:#666;font-size:12px;margin-top:6px}
.ab{color:#444;font-size:13px;margin-top:8px}
a{color:#1f3864}
.box{border:2px dashed #bbb;padding:6px 10px;margin-top:10px;font-size:13px;color:#555}
</style>"""]
for code, desc, rows, blind in blocks:
    out.append(f"<h2>BLOK {code} &nbsp;<span>{e(desc)}</span></h2>")
    for n, it in enumerate(rows, 1):
        meta = ("" if blind else
                f'<div class="m">{e(it["src"])} &middot; sira {it["rank"]}/{len(items)} '
                f'&middot; puan {it["score"]:.3f} &middot; rec {it["rec"]:.2f} '
                f'eng {it["eng"]:.2f} top {it["top"]:.2f} &middot; {e(it["date"])}</div>')
        ab = f'<div class="ab">{e(it["abs"])}</div>' if it["abs"] else \
             '<div class="ab"><i>(ozet yok)</i></div>'
        out.append(f'<div class="it"><span class="tag">{code}{n}</span>'
                   f'<span class="t"><a href="{e(it["url"])}" target="_blank">{e(it["title"])}</a></span>'
                   f'{meta}{ab}<div class="box">{code}{n} = ilgi __ / deger __</div></div>')
Path("reading_list.html").write_text("".join(out), encoding="utf-8")

print("\nreading_list.html yazildi.\n")
print("Blok A cozumu (OKUMADAN BAKMA):")
for n, it in enumerate(a, 1):
    print(f"   A{n}  sira {it['rank']:>3}  puan {it['score']:.3f}")