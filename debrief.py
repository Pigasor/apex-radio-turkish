"""
Débrief automatique : un rapport par session (essais, qualif, course).

Chaque tour bouclé est noté (temps, secteurs, validité, position, essence, arrêt).
Le rapport est réécrit à chaque tour dans debriefs/ (rien n'est perdu si tu fermes l'appli),
au format JSON + une page HTML lisible depuis le tableau de bord (bouton DÉBRIEFS).
"""

from __future__ import annotations

import html
import json
import re
import statistics
import time
from pathlib import Path

import i18n
from i18n import L

HERE = Path(__file__).parent
from paths import DATA, LOCAL  # noqa: E402
DIR = DATA / "debriefs"


def fmt(t):
    if not t:
        return "–"
    m, r = divmod(t, 60)
    return f"{int(m)}:{r:06.3f}"


class Recorder:
    def __init__(self) -> None:
        self.key = None
        self.data = None
        self._laps_seen = None
        self._lap_fuel = None
        self._lap_pit = False
        self._lap_invalid = False

    # --------------------------------------------------------------- session
    def _start(self, s) -> None:
        stamp = time.strftime("%Y-%m-%d_%Hh%M")
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{stamp}_{s.track}_{s.session}").strip("_")
        self.key = (s.track, s.car, s.session_code)
        self.data = {"file": name, "date": time.strftime("%d/%m/%Y %H:%M"), "track": s.track, "car": s.car,
                     "class": s.car_class, "session": s.session, "laps": [], "start_pos": s.class_position or s.position,
                     "end_pos": None, "issues": [], "tyres": [], "radio": [], "incidents": 0, "setup": None}
        self._laps_seen = s.laps_done
        self._lap_fuel = s.fuel

    def update(self, s, lt, beh, eng, setup_label=None) -> None:
        if not s.on_track or not s.track:
            return
        key = (s.track, s.car, s.session_code)
        if key != self.key or (self._laps_seen is not None and s.laps_done < self._laps_seen):
            self._start(s)
        self._lap_pit |= s.in_pits
        self._lap_invalid |= s.lap_invalid
        if s.laps_done == self._laps_seen:
            return
        # un tour vient d'être bouclé
        self._laps_seen = s.laps_done
        if s.last_lap and s.laps_done >= 1:
            used = (self._lap_fuel - s.fuel) if self._lap_fuel is not None and self._lap_fuel >= s.fuel else None
            self.data["laps"].append({
                "n": s.laps_done, "time": s.last_lap, "sectors": s.sectors, "valid": not self._lap_invalid and not self._lap_pit,
                "pit": self._lap_pit, "pos": s.class_position or s.position, "fuel_used": used, "fuel": s.fuel,
                "compound": s.compound, "rain": round(s.raining, 2)})
        self._lap_fuel, self._lap_pit, self._lap_invalid = s.fuel, s.in_pits, s.lap_invalid
        self.data["end_pos"] = s.class_position or s.position
        self.data["issues"] = [i["text"] + " — " + i["tip"] for i in beh.issues[:6]] if beh else []
        self.data["tyres"] = list(beh.tyre_tips[:4]) if beh else []
        self.data["radio"] = [m["text"] for m in list(eng.radio)[:60]][::-1] if eng else []
        self.data["record"] = (lt.ui(s) or {}).get("record") if lt else None
        self.data["setup"] = setup_label
        self.save()

    def save(self) -> None:
        if not self.data or not self.data["laps"]:
            return
        DIR.mkdir(parents=True, exist_ok=True)
        (DIR / f"{self.data['file']}.json").write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        (DIR / f"{self.data['file']}.html").write_text(render(self.data), encoding="utf-8")


# ------------------------------------------------------------------- listes
def listing() -> list[dict]:
    if not DIR.exists():
        return []
    out = []
    for f in sorted(DIR.glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            valid = [l["time"] for l in d["laps"] if l["valid"]]
            out.append({"file": d["file"], "date": d["date"], "track": d["track"], "session": d["session"],
                        "class": d.get("class", ""),
                        "laps": len(d["laps"]), "best": min(valid) if valid else None})
        except Exception:
            continue
    return out


def index_page() -> str:
    rows = "".join(
        f'<a class="row" href="/debrief/{html.escape(x["file"])}"><b>{html.escape(x["date"])}</b>'
        f'<span>{html.escape(x["track"])}</span><span>{html.escape(i18n.session(x["session"]))}</span>'
        f'<span>{x["laps"]} {L("tours", "laps", "tur")}</span><span class="mono">{fmt(x["best"])}</span></a>' for x in listing())
    empty = "<p class=muted>" + L("Pas encore de session enregistrée. Fais quelques tours avec Apex Radio ouvert.",
                                  "No session recorded yet. Do a few laps with Apex Radio running.",
                                  "Henüz kayıtlı seans yok. Apex Radio açıkken birkaç tur at.") + "</p>"
    return _page(L("Débriefs", "Debriefs", "Özetler"), f"<h1>{L('DÉBRIEFS', 'DEBRIEFS', 'ÖZETLER')}</h1><div class='list'>{rows or empty}</div>")


# ------------------------------------------------------------------- rendu
def render(d: dict) -> str:
    laps = d["laps"]
    valid = [l for l in laps if l["valid"]]
    times = [l["time"] for l in valid]
    best = min(times) if times else None
    clean = [t for t in times if best and t < best * 1.07]
    avg = statistics.mean(clean) if clean else None
    spread = statistics.pstdev(clean) if len(clean) > 1 else None
    fuel = [l["fuel_used"] for l in laps if l["valid"] and l.get("fuel_used")]
    pits = sum(1 for l in laps if l["pit"])
    gained = (d["start_pos"] - d["end_pos"]) if d.get("start_pos") and d.get("end_pos") else None

    def tile(label, value, sub=""):
        return f"<div class='tile'><div class='lab'>{label}</div><div class='val'>{value}</div><div class='sub'>{sub}</div></div>"

    tiles = "".join([
        tile(L("Tours", "Laps", "Turlar"), len(laps), L(f"{len(valid)} valides", f"{len(valid)} valid", f"{len(valid)} geçerli")),
        tile(L("Meilleur tour", "Best lap", "En iyi tur"), fmt(best), L(f"record circuit {fmt(d.get('record'))}", f"track record {fmt(d.get('record'))}", f"pist rekoru {fmt(d.get('record'))}")),
        tile(L("Moyenne (tours propres)", "Average (clean laps)", "Ortalama (temiz turlar)"), fmt(avg), L(f"régularité ± {spread:.2f} s", f"consistency ± {spread:.2f} s", f"istikrar ± {spread:.2f} sn") if spread else ""),
        tile(L("Essence / tour", "Fuel / lap", "Yakıt / tur"), f"{statistics.mean(fuel):.2f} L" if fuel else "–", L(f"{pits} passage(s) au stand", f"{pits} pit visit(s)", f"{pits} pit ziyareti")),
        tile("Position", f"P{d['end_pos']}" if d.get("end_pos") else "–",
             (f"{'+' if gained > 0 else ''}{gained} " + L("place(s)", "place(s)", "sıra") if gained else L("même place", "same place", "aynı sıra")) if gained is not None else ""),
    ])

    # graphique des temps au tour
    chart = ""
    if len(times) >= 2:
        W, H, pad = 900, 220, 34
        lo, hi = min(times), min(max(times), min(times) * 1.08)
        hi = hi if hi > lo else lo + 1
        xs = [l["n"] for l in valid]
        x0, x1 = min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1
        P = lambda n, t: (pad + (n - x0) / (x1 - x0) * (W - 2 * pad), H - pad - (min(t, hi) - lo) / (hi - lo) * (H - 2 * pad))
        pts = " ".join(f"{P(l['n'], l['time'])[0]:.1f},{P(l['n'], l['time'])[1]:.1f}" for l in valid)
        dots = "".join(f"<circle cx='{P(l['n'], l['time'])[0]:.1f}' cy='{P(l['n'], l['time'])[1]:.1f}' r='4' fill='{'#FFE500' if l['time'] == best else '#00E5FF'}'><title>{L('Tour', 'Lap', 'Tur')} {l['n']} : {fmt(l['time'])}</title></circle>" for l in valid)
        pit_lines = "".join(f"<line x1='{P(l['n'], lo)[0]:.1f}' x2='{P(l['n'], lo)[0]:.1f}' y1='{pad}' y2='{H - pad}' stroke='#FF2E88' stroke-dasharray='4 4'/>" for l in laps if l["pit"] and x0 <= l["n"] <= x1)
        chart = (f"<h2>{L('Temps au tour', 'Lap times', 'Tur zamanları')}</h2><svg viewBox='0 0 {W} {H}' role='img' aria-label='{L('Temps au tour', 'Lap times', 'Tur zamanları')}'>"
                 f"<line x1='{pad}' x2='{W - pad}' y1='{H - pad}' y2='{H - pad}' stroke='#1E2A38'/>"
                 f"<text x='4' y='{pad}' fill='#9AA6B8' font-size='12'>{fmt(hi)}</text><text x='4' y='{H - pad}' fill='#9AA6B8' font-size='12'>{fmt(lo)}</text>"
                 f"{pit_lines}<polyline points='{pts}' fill='none' stroke='#00E5FF' stroke-width='2'/>{dots}</svg>"
                 "<p class='muted'>" + L("Jaune : meilleur tour · tirets roses : passage au stand · tours invalides non affichés.",
                                         "Yellow: best lap · pink dashes: pit visit · invalid laps not shown.",
                                         "Sarı: en iyi tur · pembe kesik çizgi: pit ziyareti · geçersiz turlar gösterilmez.") + "</p>")

    rows = "".join(
        f"<tr class='{'' if l['valid'] else 'inv'}'><td>{l['n']}</td><td class='mono'>{fmt(l['time'])}</td>"
        + ("".join(f"<td class='mono'>{x:.3f}</td>" for x in l["sectors"]) if l["sectors"] else "<td></td>" * 3)
        + f"<td>P{l['pos']}</td><td class='mono'>{(l['fuel_used'] or 0):.2f}</td><td>{L('stand', 'pit', 'pit') if l['pit'] else (L('invalide', 'invalid', 'geçersiz') if not l['valid'] else '')}</td></tr>"
        for l in laps)
    # rythme par tranche de 5 tours propres (régularité, usure)
    pace = ""
    clean_laps = [l for l in valid if best and l["time"] < best * 1.05]
    if len(clean_laps) >= 5:
        prows, prev = [], None
        for k in range(0, len(clean_laps) - 4, 5):
            blk = clean_laps[k:k + 5]
            m = statistics.median(l["time"] for l in blk)
            sd = statistics.pstdev(l["time"] for l in blk)
            trend = "" if prev is None else (f"<span style='color:#FF2E88'>+{m - prev:.2f} s</span>" if m - prev > 0.05
                                             else f"<span style='color:#3DFFA2'>{m - prev:.2f} s</span>" if m - prev < -0.05 else "=")
            reg = L("très régulier", "very consistent", "çok istikrarlı") if sd < 0.3 else L("irrégulier", "inconsistent", "dalgalı") if sd > 0.8 else L("correct", "ok", "iyi")
            prows.append(f"<tr><td>{L('Tours', 'Laps', 'Turlar')} {blk[0]['n']}–{blk[-1]['n']}</td><td class='mono'>{fmt(m)}</td><td>{trend}</td>"
                         f"<td class='mono'>± {sd:.2f} s</td><td>{reg}</td></tr>")
            prev = m
        pace = (L("<h2>Rythme par tranche de 5 tours</h2><table><tr><th>Tours</th><th>Rythme médian</th><th>Évolution</th><th>Régularité</th><th></th></tr>",
                  "<h2>Pace per 5-lap block</h2><table><tr><th>Laps</th><th>Median pace</th><th>Trend</th><th>Consistency</th><th></th></tr>",
                  "<h2>5 turluk bloklarda tempo</h2><table><tr><th>Turlar</th><th>Medyan tempo</th><th>Eğilim</th><th>İstikrar</th><th></th></tr>")
                + "".join(prows) + "</table><p class='muted'>"
                + L("Évolution rose = plus lent que la tranche d'avant (usure, essence, trafic).",
                    "Pink trend = slower than the previous block (wear, fuel, traffic).",
                    "Pembe eğilim = önceki bloktan daha yavaş (aşınma, yakıt, trafik).") + "</p>")
    issues = "".join(f"<li>{html.escape(x)}</li>" for x in d.get("issues", []) + d.get("tyres", []))
    radio = "".join(f"<li>{html.escape(x)}</li>" for x in d.get("radio", []))
    body = f"""
<a href="/debriefs" class="back">← {L('tous les débriefs', 'all debriefs', 'tüm özetler')}</a>
<h1>{L('DÉBRIEF', 'DEBRIEF', 'ÖZET')} · {html.escape(d['track'])}</h1>
<p class="muted">{html.escape(d['date'])} · {html.escape(d['car'])} · {html.escape(i18n.session(d['session']))}{' · setup ' + html.escape(d['setup']) if d.get('setup') else ''}</p>
<div class="tiles">{tiles}</div>
{chart}
{pace}
<h2>{L('Comportement de la voiture', 'Car behaviour', 'Araç davranışı')}</h2><ul>{issues or '<li class="muted">' + L('Rien de répétitif signalé.', 'Nothing recurring reported.', 'Tekrarlayan bir sorun bildirilmedi.') + '</li>'}</ul>
<h2>{L('Tours', 'Laps', 'Turlar')}</h2><div class="scroll"><table><tr><th>{L('Tour', 'Lap', 'Tur')}</th><th>{L('Temps', 'Time', 'Zaman')}</th><th>S1</th><th>S2</th><th>S3</th><th>Pos.</th><th>{L('Essence', 'Fuel', 'Yakıt')}</th><th></th></tr>{rows}</table></div>
<h2>Radio</h2><ul class="radio">{radio}</ul>"""
    return _page(L(f"Débrief {d['track']}", f"Debrief {d['track']}", f"Özet {d['track']}"), body)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="{i18n.LANG}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="icon" href="/favicon.ico">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@900&family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#06070B;--panel:#0B0F16;--line:#1A2330;--text:#E8EDF5;--muted:#9AA6B8;--accent:#00E5FF}}
body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Inter,"Segoe UI",sans-serif;padding:24px;max-width:1100px;margin:auto}}
h1{{font-family:Orbitron,sans-serif;color:var(--accent);letter-spacing:.1em;text-shadow:0 0 14px rgba(0,229,255,.6);font-size:26px}}
h2{{font-size:13px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase;margin-top:28px}}
.muted{{color:var(--muted)}} .mono{{font-family:"JetBrains Mono",monospace}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}}
.tile{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}}
.lab{{font-size:11px;letter-spacing:.12em;color:var(--muted);text-transform:uppercase}} .val{{font-family:"JetBrains Mono",monospace;font-size:26px;font-weight:700}} .sub{{color:var(--muted);font-size:13px}}
svg{{width:100%;background:var(--panel);border:1px solid var(--line);border-radius:12px}}
table{{width:100%;border-collapse:collapse;font-size:14px}} th{{color:var(--muted);text-align:left;font-size:12px;padding:6px}} td{{padding:5px 6px;border-top:1px solid var(--line)}}
tr.inv td{{color:var(--muted)}} .scroll{{overflow-x:auto}}
ul{{padding-left:20px}} .radio li{{color:var(--muted);margin:3px 0}}
a{{color:var(--accent)}} .back{{text-decoration:none}}
.list{{display:flex;flex-direction:column;gap:8px}} .row{{display:grid;grid-template-columns:160px 1fr 110px 90px 110px;gap:10px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;color:var(--text);text-decoration:none}}
.row:hover{{border-color:var(--accent)}}
.nav{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}} .nav a{{text-decoration:none;color:var(--text);border:1px solid var(--line);background:var(--panel);border-radius:999px;padding:6px 14px;font-size:13px;font-weight:600;letter-spacing:.04em}}
.nav a:hover{{border-color:var(--accent)}} .nav a.home{{color:#06070B;background:var(--accent);border-color:var(--accent)}} .nav a.nrec{{color:#FFE500}}
@media(max-width:700px){{.row{{grid-template-columns:1fr 1fr}}}}
</style></head><body>
<nav class="nav"><a class="home" href="/">← {L('TABLEAU DE BORD', 'DASHBOARD', 'GÖSTERGE PANELİ')}</a><a class="nrec" href="/records">{L('RECORDS', 'RECORDS', 'REKORLAR')}</a><a href="/debriefs">{L('DÉBRIEFS', 'DEBRIEFS', 'ÖZETLER')}</a></nav>
{body}</body></html>"""


# ------------------------------------------------------------------- records par circuit
def _thumb(track: str) -> str:
    """Petit dessin du circuit (si la carte a été apprise)."""
    try:
        from trackmap import _fname
        pts = [p for p in json.loads(_fname(track).read_text(encoding="utf-8"))["points"] if p]
    except Exception:
        return "<div class='thumb muted'>" + L("pas de carte", "no map", "harita yok") + "</div>"
    xs, zs = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
    W, H, pad = 160, 100, 8
    k = min((W - 2 * pad) / max(1, x1 - x0), (H - 2 * pad) / max(1, z1 - z0))
    ox, oy = pad + ((W - 2 * pad) - (x1 - x0) * k) / 2, pad + ((H - 2 * pad) - (z1 - z0) * k) / 2
    path = " ".join(f"{(p[0] - x0) * k + ox:.1f},{(z1 - p[1]) * k + oy:.1f}" for p in pts[::2])
    return (f"<svg class='thumb' viewBox='0 0 {W} {H}' role='img' aria-label='{L("Tracé de", "Layout of", "Pist çizimi:")} {html.escape(track)}'>"
            f"<polygon points='{path}' fill='none' stroke='#00E5FF' stroke-width='3' stroke-linejoin='round'/></svg>")


def _spark(pts: list) -> str:
    """Petit graphique : meilleur tour de chaque session sur ce circuit (plus bas = plus rapide)."""
    if len(pts) < 2:
        return "<div class='muted' style='font-size:12px;margin-top:6px'>" + L("Progression : il faut au moins 2 sessions.", "Progress: needs at least 2 sessions.", "Gelişim: en az 2 seans gerekli.") + "</div>"
    pts = pts[-12:]
    W, H, pad = 420, 70, 10
    ts = [p[1] for p in pts]
    lo, hi = min(ts), max(ts)
    hi = hi if hi - lo > 0.05 else lo + 0.5
    X = lambda i: pad + i * (W - 2 * pad) / (len(pts) - 1)
    Y = lambda v: pad + (v - lo) / (hi - lo) * (H - 2 * pad)
    line = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, (_, v, _) in enumerate(pts))
    dots = "".join(f"<circle cx='{X(i):.1f}' cy='{Y(v):.1f}' r='4' fill='{'#FFE500' if v == lo else '#00E5FF'}'>"
                   f"<title>{html.escape(d)} · {html.escape(s)} : {fmt(v)}</title></circle>" for i, (d, v, s) in enumerate(pts))
    gain = pts[0][1] - pts[-1][1]
    txt = (L(f"{gain:.2f} s gagnées depuis le {html.escape(pts[0][0])}", f"{gain:.2f} s gained since {html.escape(pts[0][0])}", f"{html.escape(pts[0][0])} tarihinden beri {gain:.2f} sn kazanç") if gain > 0.005
           else L(f"{-gain:.2f} s de plus que le {html.escape(pts[0][0])}", f"{-gain:.2f} s slower than on {html.escape(pts[0][0])}", f"{html.escape(pts[0][0])} tarihine göre {-gain:.2f} sn daha yavaş") if gain < -0.005
           else L("stable", "steady", "sabit"))
    return (f"<svg class='spark' viewBox='0 0 {W} {H}' role='img' aria-label='{L("Progression des meilleurs tours", "Best lap progress", "En iyi tur gelişimi")}'>"
            f"<polyline points='{line}' fill='none' stroke='#00E5FF' stroke-width='2'/>{dots}</svg>"
            f"<div class='muted' style='font-size:12px'>{L('Progression', 'Progress', 'Gelişim')} ({len(pts)} sessions) : {txt}</div>")


def records_page() -> str:
    from alerts import class_name
    try:
        recs = json.loads((DATA / "records.json").read_text(encoding="utf-8"))
    except Exception:
        recs = {}
    # progression : meilleur tour de chaque session (pris dans les débriefs)
    prog = {}
    for x in reversed(listing()):
        if x.get("best") and "démo" not in x["track"].lower():
            prog.setdefault((x["track"], x.get("class", "")), []).append((x["date"][:5], x["best"], i18n.session(x["session"])))
    rows = []
    for key, r in sorted(recs.items(), key=lambda kv: kv[0].lower()):
        track, _, cls = key.partition(" | ")
        if "démo" in track.lower():
            continue
        sec = r.get("sectors") or []
        best = r.get("best_sectors") or []
        ideal = sum(best) if len(best) == 3 else None
        sec_html = " · ".join(f"S{i + 1} {x:.3f}" for i, x in enumerate(sec)) if sec else ""
        # message prêt à coller dans le Discord (#chronos-laptimes)
        share = (f"🏁 {track} · {class_name(cls)}" + (f" · {r.get('car')}" if r.get('car') else "") + f"\n⏱️ {fmt(r.get('lap'))}"
                 + (f"  ({' · '.join(f'S{i + 1} {x:.3f}' for i, x in enumerate(sec))})" if sec else "")
                 + (f"\n🎯 {L('Tour idéal', 'Ideal lap', 'İdeal tur')} {fmt(ideal)}" if ideal else "")
                 + f"\n📅 {r.get('date', '')} · Apex Radio")
        rows.append(f"""<div class="rec">{_thumb(track)}
  <div class="info"><div class="tr">{html.escape(track)}</div>
    <div class="muted">{html.escape(class_name(cls))} · {html.escape(r.get('car', ''))} · {html.escape(r.get('date', ''))}</div>
    <div class="muted mono" style="font-size:13px">{sec_html}</div>{_spark(prog.get((track, cls), []))}</div>
  <div class="best"><div class="lab">{L('Meilleur tour', 'Best lap', 'En iyi tur')}</div><div class="val">{fmt(r.get('lap'))}</div>
    <div class="sub">{L('tour idéal', 'ideal lap', 'ideal tur')} {fmt(ideal)}</div>
    <button class="share" data-share="{html.escape(share, quote=True)}">📋 {L('Partager sur Discord', 'Share on Discord', "Discord'da paylaş")}</button></div></div>""")
    css = """<style>.rec{display:grid;grid-template-columns:170px 1fr 180px;gap:16px;align-items:center;background:var(--panel);
border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-bottom:10px}.thumb{width:160px;height:100px;border:0;background:none;
display:flex;align-items:center;justify-content:center}
.spark{width:100%;max-width:420px;height:70px;margin-top:6px;border-radius:8px}.tr{font-size:18px;font-weight:600}.best{text-align:right}
.best .val{font-family:"JetBrains Mono",monospace;font-size:28px;font-weight:700;color:#FFE500;text-shadow:0 0 12px rgba(255,229,0,.5)}
.share{margin-top:8px;font:inherit;font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);background:transparent;color:inherit;cursor:pointer}
.share:hover{border-color:#FFE500}.share.ok{border-color:#3DFFA2;color:#3DFFA2}
@media(max-width:700px){.rec{grid-template-columns:1fr}.best{text-align:left}}</style>
<script>document.addEventListener("click", async e => {
  const b = e.target.closest(".share"); if (!b) return;
  const t = b.dataset.share, old = b.textContent;
  let ok = false;
  try { await navigator.clipboard.writeText(t); ok = true; } catch (_) {
    const a = document.createElement("textarea"); a.value = t; document.body.appendChild(a); a.select();
    try { ok = document.execCommand("copy"); } catch (__) {} a.remove(); }
  b.textContent = ok ? "✓ """ + L("Copié ! Colle-le dans #chronos-laptimes", "Copied! Paste it in #chronos-laptimes", "Kopyalandı! #chronos-laptimes kanalına yapıştır") + """" : old;
  b.classList.toggle("ok", ok); setTimeout(() => { b.textContent = old; b.classList.remove("ok"); }, 4000);
});</script>"""
    body = css + L("<h1>MEILLEURS TOURS</h1><p class='muted'>Ton record sur chaque circuit, par catégorie (tours valides uniquement).</p>",
                   "<h1>BEST LAPS</h1><p class='muted'>Your record on each track, by class (valid laps only).</p>",
                   "<h1>EN İYİ TURLAR</h1><p class='muted'>Her pistteki rekorun, sınıfa göre (yalnızca geçerli turlar).</p>") + \
        ("".join(rows) or "<p class='muted'>" + L("Pas encore de record : fais quelques tours valides avec Apex Radio ouvert.",
                                                 "No record yet: do a few valid laps with Apex Radio running.",
                                                 "Henüz rekor yok: Apex Radio açıkken birkaç geçerli tur at.") + "</p>")
    return _page(L("Meilleurs tours", "Best laps", "En iyi turlar"), body)
