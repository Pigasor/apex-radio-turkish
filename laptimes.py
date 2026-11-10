"""
Chronos : records personnels par circuit et comparaison par secteurs.

- records.json garde, pour chaque circuit + catégorie, ton meilleur tour valide
  (avec ses secteurs), la voiture et la date, plus tes meilleurs secteurs isolés.
- Après chaque tour : écart secteur par secteur avec la voiture devant et
  derrière dans ta catégorie (sur leur dernier tour).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from telemetry import Snapshot
import i18n
from i18n import L as tr, dec, en

HERE = Path(__file__).parent
from paths import DATA, LOCAL  # noqa: E402
RECORDS = DATA / "records.json"
TRACES = DATA / "traces"     # temps de passage du tour record (pour le delta en direct)
BINS = 200
COMPARE_EVERY = 5        # annonce de comparaison tous les 5 tours
MAX_SECTOR_GAP = 2.5     # au-delà, la comparaison n'a pas de sens (arrêt au stand, incident, trafic)
NOTABLE = 0.25           # écart de secteur (s) jugé important


def fmt_lap(t: float | None) -> str:
    if not t:
        return "–"
    m, r = divmod(t, 60)
    return f"{int(m)}:{r:06.3f}"


def _say_lap(t: float) -> str:
    m, r = divmod(t, 60)
    return dec(tr(f"{int(m)} minute {r:.2f}", f"{int(m)} minute {r:.2f}", f"{int(m)} dakika {r:.2f}")) if m else dec(tr(f"{r:.2f} secondes", f"{r:.2f} seconds", f"{r:.2f} saniye"))


MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def _say_date(d: str) -> str:
    try:
        j, m, a = d.split("/")
        if i18n.tr():
            return f"{int(j)} " + ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül",
                                   "Ekim", "Kasım", "Aralık"][int(m) - 1]
        if en():
            return ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                    "October", "November", "December"][int(m) - 1] + f" {int(j)}"
        return f"{int(j)} {MOIS[int(m) - 1]}"
    except Exception:
        return d


def _dixiemes(x: float) -> str:
    d = round(abs(x) * 10)
    if d == 0:
        return tr("moins d'un dixième", "less than a tenth", "saniyenin onda birinden az")
    if d >= 10:
        return dec(tr(f"{abs(x):.1f} seconde{'s' if abs(x) >= 2 else ''}", f"{abs(x):.1f} seconds", f"{abs(x):.1f} saniye"))
    return tr(f"{d} dixième" + ("s" if d > 1 else ""), f"{d} tenth" + ("s" if d > 1 else ""), f"onda {d} saniye")


class LapTimes:
    def __init__(self) -> None:
        try:
            self.records = json.loads(RECORDS.read_text(encoding="utf-8"))
        except Exception:
            self.records = {}
        self._laps_seen = None
        self._invalid = False
        self._in_pits = False
        self._lap_count = 0
        self.last = {}          # dernière analyse pour l'UI / les questions
        self.trace = [None] * BINS      # tour en cours : temps de passage par case
        self.ref = None                 # tour de référence (record, sinon meilleur tour de la session)
        self.ref_kind = ""
        self.ref_key = None
        self.session_best = None
        self.delta = None

    # ------------------------------------------------------------- delta en direct
    def _trace_file(self, key: str) -> Path:
        import re
        return TRACES / (re.sub(r"[^A-Za-z0-9_-]+", "_", key).strip("_") + ".json")

    def _load_ref(self, s: Snapshot) -> None:
        key = self.key(s)
        if key == self.ref_key:
            return
        self.ref_key, self.ref, self.ref_kind, self.session_best = key, None, "", None
        try:
            self.ref = json.loads(self._trace_file(key).read_text(encoding="utf-8"))["t"]
            self.ref_kind = "record"
        except Exception:
            pass

    @staticmethod
    def _at(tr: list, f: float) -> float | None:
        x = f * BINS
        i = int(x)
        if i >= BINS:
            return None
        a = tr[i]
        b = tr[i + 1] if i + 1 < BINS else None
        if a is None:
            return None
        return a + (b - a) * (x - i) if b is not None and b >= a else a

    def _feed_trace(self, s: Snapshot) -> None:
        self._load_ref(s)
        if s.lap_time is None or s.in_pits:
            self.delta = None
            return
        i = min(BINS - 1, int(s.lap_frac * BINS))
        if self.trace[i] is None:
            self.trace[i] = s.lap_time
        r = self._at(self.ref, s.lap_frac) if self.ref and s.lap_frac > 0.02 else None
        self.delta = round(s.lap_time - r, 2) if r is not None and abs(s.lap_time - r) < 30 else None

    def _close_trace(self, s: Snapshot, lap: float, valid: bool, is_record: bool) -> None:
        tr, self.trace = self.trace, [None] * BINS
        full = sum(1 for x in tr if x is not None) >= 40
        if not valid or not full:
            return
        tr[-1] = tr[-1] if tr[-1] is not None else lap
        # trous : on bouche par interpolation
        last = 0.0
        for i in range(BINS):
            if tr[i] is None:
                j = next((k for k in range(i + 1, BINS) if tr[k] is not None), None)
                nxt = tr[j] if j is not None else lap
                tr[i] = last + (nxt - last) / ((j or BINS) - i + 1)
            last = tr[i]
        if is_record:
            TRACES.mkdir(parents=True, exist_ok=True)
            self._trace_file(self.key(s)).write_text(json.dumps({"lap": lap, "t": [round(x, 3) for x in tr]}), encoding="utf-8")
            self.ref, self.ref_kind = tr, "record"
        elif self.ref_kind != "record" and (self.session_best is None or lap < self.session_best):
            self.ref, self.ref_kind = tr, "session"
        if self.session_best is None or lap < self.session_best:
            self.session_best = lap

    def key(self, s: Snapshot) -> str:
        return f"{s.track} | {s.car_class or 'Inconnue'}"

    def record(self, s: Snapshot) -> dict | None:
        return self.records.get(self.key(s))

    def _save(self) -> None:
        RECORDS.write_text(json.dumps(self.records, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------- analyse
    @staticmethod
    def compare(mine: list | None, other: dict | None) -> list | None:
        """Écart par secteur : négatif = tu es plus rapide."""
        if not mine or not other or not other.get("sectors") or other.get("in_pits"):
            return None
        d = [a - b for a, b in zip(mine, other["sectors"])]
        if any(abs(x) > MAX_SECTOR_GAP for x in d):
            return None
        return d

    def update(self, s: Snapshot, say) -> None:
        if not s.on_track:
            return
        if s.laps_done == self._laps_seen:
            self._feed_trace(s)
        if self._laps_seen is None or s.laps_done < self._laps_seen:
            self._laps_seen = s.laps_done
            self._invalid = self._in_pits = False
            self.trace = [None] * BINS
            return
        if s.laps_done == self._laps_seen:
            self._invalid |= s.lap_invalid
            self._in_pits |= s.in_pits
            return

        # un tour vient d'être bouclé
        invalid, in_pits = self._invalid, self._in_pits
        self._laps_seen = s.laps_done
        self._invalid = s.lap_invalid
        self._in_pits = s.in_pits
        self._lap_count += 1
        lap, sec = s.last_lap, s.sectors
        rec0 = self.record(s)
        is_rec = bool(lap) and not invalid and not in_pits and s.laps_done > 1 and (rec0 is None or lap < rec0["lap"])
        if lap:
            self._close_trace(s, lap, not invalid and not in_pits, is_rec)
        self._feed_trace(s)
        if not lap or s.laps_done <= 1:
            return
        vs_old = [a - b for a, b in zip(sec, rec0["sectors"])] if (sec and rec0 and rec0.get("sectors")) else None

        et = s.elapsed
        rec = self.record(s)
        valid = not invalid and not in_pits

        # --- record perso
        if valid and (rec is None or lap < rec["lap"]):
            old = rec["lap"] if rec else None
            new = {"lap": lap, "sectors": sec, "car": s.car, "date": time.strftime("%d/%m/%Y"),
                   "best_sectors": (rec or {}).get("best_sectors") or sec}
            self.records[self.key(s)] = new
            rec = new
            if old:
                say(None, tr(f"Nouveau record perso ici ! {_say_lap(lap)}, tu gagnes {_dixiemes(old - lap)}.",
                             f"New personal best here! {_say_lap(lap)}, {_dixiemes(old - lap)} quicker.",
                             f"Burada yeni kişisel rekor! {_say_lap(lap)}, {_dixiemes(old - lap)} daha hızlı.")
                    + (self._sector_bits(vs_old) if s.session != "course" else ""), elapsed=et)
            elif self._lap_count > 1:
                say(None, tr(f"Premier chrono de référence sur ce circuit : {_say_lap(lap)}.",
                             f"First reference lap on this track: {_say_lap(lap)}.",
                             f"Bu pistteki ilk referans tur: {_say_lap(lap)}."), elapsed=et)
        if valid and sec and rec:
            best = rec.get("best_sectors") or sec
            rec["best_sectors"] = [min(a, b) for a, b in zip(best, sec)]
        if valid:
            self._save()

        # --- comparaison devant / derrière
        d_ahead = self.compare(sec, s.ahead) if valid else None
        d_behind = self.compare(sec, s.behind) if valid else None
        self.last = {"lap": lap, "sectors": sec, "valid": valid, "ahead": d_ahead, "behind": d_behind,
                     "ahead_name": (s.ahead or {}).get("name"), "behind_name": (s.behind or {}).get("name"),
                     "vs_record": [a - b for a, b in zip(sec, rec["sectors"])] if (sec and rec and rec.get("sectors")) else None}

        if invalid and not in_pits:
            say(None, tr("Tour invalidé, attention aux limites de piste.", "Lap invalidated, watch the track limits.", "Tur geçersiz sayıldı, pist sınırlarına dikkat."), elapsed=et)
        elif valid and s.session in ("qualif", "essais") and rec0 and lap >= rec0["lap"] and vs_old:
            # qualif / essais : où tu as gagné et perdu par rapport à ton record
            say(None, tr(f"{_say_lap(lap)}, à {_dixiemes(lap - rec0['lap'])} du record.",
                         f"{_say_lap(lap)}, {_dixiemes(lap - rec0['lap'])} off your best.",
                         f"{_say_lap(lap)}, en iyi turundan {_dixiemes(lap - rec0['lap'])} geride.") + self._sector_bits(vs_old), elapsed=et)
        elif s.session == "course" and self._lap_count % COMPARE_EVERY == 0:
            text = self.summary("ahead") or self.summary("behind")
            if text:
                say(None, text, elapsed=et)

    # ------------------------------------------------------------- phrases
    @staticmethod
    def _sector_bits(d: list | None) -> str:
        if not d:
            return ""
        parts = []
        for i, x in enumerate(d):
            if abs(x) < 0.1:
                continue
            q = _dixiemes(x)
            pl = "s" if q.endswith("s") else ""
            parts.append(tr(f"{q} {'gagné' if x < 0 else 'perdu'}{pl} au secteur {i + 1}",
                            f"{q} {'gained' if x < 0 else 'lost'} in sector {i + 1}",
                            f"{i + 1}. sektörde {q} {'kazanç' if x < 0 else 'kayıp'}"))
        return (" " + ", ".join(parts).capitalize() + ".") if parts else ""

    def _one(self, deltas: list | None, who: str) -> str | None:
        if not deltas:
            return None
        lose = max(range(3), key=lambda i: deltas[i])
        gain = min(range(3), key=lambda i: deltas[i])
        total = sum(deltas)
        parts = []
        if deltas[lose] > 0.05:
            parts.append(tr(f"tu perds {_dixiemes(deltas[lose])} dans le secteur {lose + 1}",
                            f"you lose {_dixiemes(deltas[lose])} in sector {lose + 1}",
                            f"{lose + 1}. sektörde {_dixiemes(deltas[lose])} kaybediyorsun"))
        if deltas[gain] < -0.05:
            parts.append(tr(f"tu reprends {_dixiemes(deltas[gain])} dans le {gain + 1}",
                            f"you gain {_dixiemes(deltas[gain])} in sector {gain + 1}",
                            f"{gain + 1}. sektörde {_dixiemes(deltas[gain])} kazanıyorsun"))
        if not parts:
            return tr(f"Même rythme que {who}.", f"Same pace as {who}.", f"Tempon {who} ile aynı.")
        tot = tr(f"Au total {'tu gagnes' if total < 0 else 'tu perds'} {_dixiemes(total)} au tour.",
                 f"Overall you {'gain' if total < 0 else 'lose'} {_dixiemes(total)} a lap.",
                 f"Toplamda tur başına {_dixiemes(total)} {'kazanıyorsun' if total < 0 else 'kaybediyorsun'}.")
        return tr(f"Sur {who}, ", f"Against {who}, ", f"Karşılaştırma, {who}: ") + tr(" et ", " and ", " ve ").join(parts) + ". " + tot

    def summary(self, which: str = "both") -> str:
        L = self.last
        if not L:
            return ""
        out = []
        if which in ("both", "ahead"):
            t = self._one(L.get("ahead"), tr("la voiture devant", "the car ahead", "öndeki araç"))
            if t:
                out.append(t)
        if which in ("both", "behind"):
            t = self._one(L.get("behind"), tr("la voiture derrière", "the car behind", "arkadaki araç"))
            if t:
                out.append(t)
        return " ".join(out)

    def record_text(self, s: Snapshot) -> str:
        rec = self.record(s)
        if not rec:
            return tr("Pas encore de record sur ce circuit dans cette catégorie.", "No record on this track in this class yet.", "Bu pistte bu sınıfta henüz rekor yok.")
        out = tr(f"Ton record ici, c'est {_say_lap(rec['lap'])}, le {_say_date(rec['date'])}.",
                 f"Your record here is {_say_lap(rec['lap'])}, set on {_say_date(rec['date'])}.",
                 f"Buradaki rekorun {_say_lap(rec['lap'])}, tarihi {_say_date(rec['date'])}.")
        if s.best_lap:
            d = s.best_lap - rec["lap"]
            out += tr(" Tu es dessus aujourd'hui.", " You matched it today.", " Bugün onu egale ettin.") if abs(d) < 0.001 else \
                tr(f" Aujourd'hui tu es à {_dixiemes(d)}.", f" Today you're {_dixiemes(d)} off.", f" Bugün {_dixiemes(d)} gerisindesin.")
        bs = rec.get("best_sectors")
        if bs:
            out += tr(f" Avec tes meilleurs secteurs, le tour idéal serait {_say_lap(sum(bs))}.",
                      f" With your best sectors, the ideal lap would be {_say_lap(sum(bs))}.",
                      f" En iyi sektörlerinle ideal tur {_say_lap(sum(bs))} olurdu.")
        return out

    def ui(self, s: Snapshot) -> dict:
        rec = self.record(s) or {}
        return {"record": rec.get("lap"), "record_sectors": rec.get("sectors"), "record_date": rec.get("date"),
                "ideal": sum(rec["best_sectors"]) if rec.get("best_sectors") else None,
                "last": self.last, "delta": self.delta, "delta_ref": self.ref_kind}
