"""
Tactique de course :
- Penalties : annonce des pénalités à purger (drive-through / stop-go) et des tours qui restent.
- Undercut  : quand la voiture devant / derrière s'arrête, ou quand tu peux tenter de la passer au stand.
- Pace      : tous les 5 tours propres, une phrase sur ton rythme, ta régularité et l'usure.
- FuelBook  : mémorise ta conso et la durée des courses par circuit pour calculer l'essence de départ.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

from i18n import L, dec

HERE = Path(__file__).parent
from paths import DATA, LOCAL  # noqa: E402
FUELBOOK = DATA / "fuelbook.json"
PENALTY_LAPS = 3          # LMU laisse normalement 3 tours pour purger une pénalité
TACTIC_GAP = 45.0         # s : au plus une annonce tactique toutes les 45 s
PACE_EVERY = 5


def _lap(t: float) -> str:
    m, r = divmod(t, 60)
    return dec(L(f"{int(m)} minute {r:.1f}", f"{int(m)} minute {r:.1f}", f"{int(m)} dakika {r:.1f}")) if m else dec(L(f"{r:.1f} secondes", f"{r:.1f} seconds", f"{r:.1f} saniye"))


def _dix(x: float) -> str:
    d = round(abs(x) * 10)
    if d >= 10:
        return dec(L(f"{abs(x):.1f} seconde" + ("s" if abs(x) >= 2 else ""), f"{abs(x):.1f} seconds", f"{abs(x):.1f} saniye"))
    return L(f"{max(1, d)} dixième" + ("s" if d > 1 else ""), f"{max(1, d)} tenth" + ("s" if d > 1 else ""), f"onda {max(1, d)} saniye")


# ===================================================================== pénalités
class Penalties:
    def __init__(self) -> None:
        self.count = 0
        self.given_lap = None
        self.said_lap = None
        self.key = None

    def update(self, s, say) -> None:
        if not s.on_track:
            return
        key = (s.track, s.session_code)
        if key != self.key:
            self.key, self.count, self.given_lap = key, s.penalties, (s.laps_done if s.penalties else None)
            return
        n = s.penalties
        if n > self.count:
            self.given_lap = self.said_lap = s.laps_done
            say(None, L("Pénalité ! Il faut la purger aux stands, regarde l'écran pour savoir si c'est un drive-through "
                        f"ou un stop and go. Normalement tu as {PENALTY_LAPS} tours.",
                        "Penalty! You need to serve it in the pits, check the screen for drive-through or stop and go. "
                        f"You normally have {PENALTY_LAPS} laps.",
                        "Ceza aldın! Pite girip cezanı çekmen gerekiyor. Pitten geçiş cezası mı, dur-kalk cezası mı, ekrandan bak. "
                        f"Normalde {PENALTY_LAPS} turun var."), urgent=True)
        elif n < self.count:
            if n == 0:
                self.given_lap = None
                say(None, L("Pénalité purgée, c'est bon.", "Penalty served, all good.", "Ceza çekildi, her şey yolunda."))
            else:
                say(None, L(f"Une pénalité purgée, il en reste {n}.", f"One penalty served, {n} left.", f"Bir ceza çekildi, {n} ceza kaldı."))
        elif n and self.given_lap is not None and s.laps_done != self.said_lap and not s.in_pits:
            self.said_lap = s.laps_done
            left = PENALTY_LAPS - (s.laps_done - self.given_lap)
            if left == 1:
                say(None, L("Dernier tour pour purger la pénalité, rentre au stand à la fin de ce tour !",
                            "Last lap to serve the penalty, box at the end of this lap!",
                            "Cezayı çekmek için son tur, bu turun sonunda pite gir!"), urgent=True)
            elif left > 1:
                say(None, L(f"Pénalité toujours à purger, plus que {left} tours.", f"Penalty still to serve, {left} laps left.", f"Ceza hâlâ bekliyor, {left} tur kaldı."))
        self.count = n

    def ui(self, s) -> dict:
        left = None
        if s.penalties and self.given_lap is not None:
            left = max(0, PENALTY_LAPS - (s.laps_done - self.given_lap))
        return {"count": s.penalties, "laps_left": left}


# ===================================================================== undercut / overcut
class Undercut:
    def __init__(self) -> None:
        self.prev = {"ahead": (None, False), "behind": (None, False)}
        self.stint = None
        self.offered = False
        self.pitted = set()     # voitures déjà passées au stand pendant ce relais
        self.last = -99.0

    def _say(self, say, text: str) -> None:
        now = time.monotonic()
        if now - self.last < TACTIC_GAP:
            return
        self.last = now
        say(None, text)

    def update(self, s, plan, say) -> None:
        if not s.on_track or s.session != "course" or s.game_phase != 5:
            self.prev = {"ahead": (None, False), "behind": (None, False)}
            return
        if s.pit_stops != self.stint:
            self.stint, self.offered, self.pitted = s.pit_stops, False, set()
        for side in ("ahead", "behind"):
            car = getattr(s, side) or {}
            name, pits = car.get("name"), bool(car.get("in_pits"))
            pname, ppits = self.prev[side]
            self.prev[side] = (name, pits)
            if s.in_pits or not name or name != pname or not pits or ppits:
                continue
            # cette voiture vient d'entrer au stand
            self.pitted.add(name)
            if side == "ahead":
                self._ahead_pits(s, plan, say, name)
            else:
                self._behind_pits(s, plan, say)
        # avant que la voiture devant ne s'arrête : tenter l'undercut ?
        if (not self.offered and plan.ready and plan.stops_needed and plan.laps_until_box in (2, 3)
                and plan.change_tyres and s.gap_class_ahead and s.gap_class_ahead < 2.5
                and s.ahead and not s.ahead.get("in_pits") and s.ahead.get("name") not in self.pitted):
            self.offered = True
            self._say(say, L(f"Tu es à {dec(f'{s.gap_class_ahead:.1f}')} seconde de {s.ahead.get('name') or 'la voiture devant'}. "
                             "Si tu rentres un tour avant lui avec des pneus neufs, tu peux le passer à l'undercut. "
                             "Demande-moi de préparer le stand si tu veux y aller.",
                             f"You're {s.gap_class_ahead:.1f} seconds behind {s.ahead.get('name') or 'the car ahead'}. "
                             "Pit a lap before him on new tyres and you can undercut him. "
                             "Ask me to prepare the pit stop if you want to go for it.",
                             f"{s.ahead.get('name') or 'Öndeki araç'} ile aranda {s.gap_class_ahead:.1f} saniye var. " "Ondan bir tur önce pite girip yeni lastik takarsan undercut yapabilirsin. " "Denemek istersen pit stopu hazırlamamı söyle."))

    def _ahead_pits(self, s, plan, say, name) -> None:
        if not plan.ready:
            return
        if not plan.stops_needed:
            self._say(say, L(f"{name} rentre au stand. Toi tu vas au bout sans t'arrêter : tu prends sa place.",
                             f"{name} is pitting. You go to the end without stopping: you take his place.",
                             f"{name} pite giriyor. Sen durmadan sona kadar gidiyorsun: onun yerini alıyorsun."))
        elif plan.laps_until_box is not None and plan.laps_until_box >= 2:
            n = min(plan.laps_until_box, 3)
            self._say(say, L(f"{name} rentre au stand. Pousse à fond les {n} prochains tours pendant qu'il ressort "
                             "avec des pneus froids : tu peux le passer à l'overcut.",
                             f"{name} is pitting. Push hard for the next {n} laps while he's on cold tyres: "
                             "you can overcut him.",
                             f"{name} pite giriyor. O soğuk lastikteyken önümüzdeki {n} tur boyunca bastır: " "overcut yapabilirsin."))
        else:
            self._say(say, L(f"{name} rentre au stand en même temps que toi. Arrêt propre et sortie des stands "
                             "nickel, tu peux ressortir devant.",
                             f"{name} is pitting at the same time. Clean stop and a tidy pit exit, you can come out ahead.",
                             f"{name} seninle aynı anda pite giriyor. Temiz bir pit stop ve düzgün bir çıkışla önünde çıkabilirsin."))

    def _behind_pits(self, s, plan, say) -> None:
        if not plan.ready or not plan.stops_needed or plan.laps_until_box is None:
            return
        g = s.gap_class_behind
        if g is not None and g < 5 and plan.laps_until_box <= 3:
            self._say(say, L(f"La voiture derrière rentre au stand, elle était à {dec(f'{g:.1f}')} seconde. "
                             "Rentre au prochain tour pour couvrir l'undercut, ou pousse fort ce tour-ci.",
                             f"The car behind is pitting, it was {g:.1f} seconds back. "
                             "Box next lap to cover the undercut, or push hard this lap.",
                             f"Arkadaki araç pite giriyor, {g:.1f} saniye gerideydi. " "Undercut'ı karşılamak için gelecek tur pite gir ya da bu tur sıkı bastır."))


# ===================================================================== rythme
class Pace:
    def __init__(self) -> None:
        self.done = 0
        self.key = None
        self.blocks = []        # pour le débrief : [(tours, moyenne, écart type)]

    def update(self, s, eng, say) -> None:
        if not s.on_track:
            return
        key = (s.track, s.session_code)
        if key != self.key:
            self.key, self.done, self.blocks = key, 0, []
        if s.session != "course" or s.game_phase != 5:
            return
        laps = [l.time for l in eng.laps if l.time and not l.dirty and l.number > 0]
        best = min(laps) if laps else None
        laps = [t for t in laps if best and t < best * 1.05]      # pas les tours avec incident
        n = len(laps) // PACE_EVERY
        if n <= self.done:
            return
        self.done = n
        last = laps[-PACE_EVERY:]
        prev = laps[-2 * PACE_EVERY:-PACE_EVERY] if len(laps) >= 2 * PACE_EVERY else None
        med, sd = statistics.median(last), statistics.pstdev(last)
        self.blocks.append((len(laps), med, sd))
        parts = [L(f"Rythme moyen {_lap(med)} sur tes 5 derniers tours", f"Average pace {_lap(med)} over your last 5 laps", f"Son 5 turda ortalama tempo {_lap(med)}")]
        if prev:
            d = med - statistics.median(prev)
            if d > 0.25:
                wear = min(s.tyre_wear) * 100 if s.tyre_wear else None
                parts.append(L(f"tu perds {_dix(d)} par tour par rapport aux 5 d'avant", f"you're losing {_dix(d)} a lap versus the 5 before", f"önceki 5 tura göre tur başına {_dix(d)} kaybediyorsun")
                             + (L(f", les pneus sont à {wear:.0f} %", f", tyres at {wear:.0f} %", f", lastikler %{wear:.0f}") if wear is not None else ""))
            elif d < -0.25:
                parts.append(L(f"tu as gagné {_dix(d)} par tour, continue", f"you've gained {_dix(d)} a lap, keep it up", f"tur başına {_dix(d)} kazandın, böyle devam"))
            else:
                parts.append(L("rythme stable", "steady pace", "tempo sabit"))
        if sd < 0.3:
            parts.append(L("très régulier", "very consistent", "çok istikrarlı"))
        elif sd > 0.8:
            parts.append(L("un peu irrégulier, vise la régularité", "a bit inconsistent, aim for consistency", "biraz dalgalı, istikrara odaklan"))
        say(None, ", ".join(parts) + ".")


# ===================================================================== essence de départ
class FuelBook:
    def __init__(self) -> None:
        try:
            self.data = json.loads(FUELBOOK.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}
        self._race_key = None
        self._last_save = 0.0

    def _save(self) -> None:
        try:
            FUELBOOK.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def update(self, s, plan, eng) -> None:
        if not s.on_track or not s.track or s.in_pits:
            return
        d = self.data.setdefault(s.track, {})
        changed = False
        clean = [l for l in eng.laps if l.time and not l.dirty and l.number > 0]
        if plan.fuel_per_lap and len(clean) >= 3 and s.raining < 0.1:
            d.update({"fpl": round(plan.fuel_per_lap, 3), "avg": round(plan.avg_lap or 0, 3) or d.get("avg"),
                      "cap": s.fuel_capacity or d.get("cap"), "car": s.car, "class": s.car_class,
                      "date": time.strftime("%d/%m/%Y")})
            changed = True
        if s.session == "course" and s.game_phase == 5 and (s.track, s.session_code) != self._race_key:
            self._race_key = (s.track, s.session_code)
            if s.max_laps:
                d["race_laps"] = s.max_laps
                d.pop("race_min", None)
            elif s.time_left:
                d["race_min"] = int(round((s.elapsed + s.time_left) / 60))
                d.pop("race_laps", None)
            changed = True
        if changed and time.monotonic() - self._last_save > 30:
            self._last_save = time.monotonic()
            self._save()

    def set_race(self, track: str, minutes: int | None, laps: int | None = None) -> None:
        d = self.data.setdefault(track, {})
        d.pop("race_min", None); d.pop("race_laps", None)
        if laps:
            d["race_laps"] = int(laps)
        elif minutes:
            d["race_min"] = int(minutes)
        self._save()

    def suggest(self, track: str) -> dict:
        d = self.data.get(track, {})
        out = {"fpl": d.get("fpl"), "avg": d.get("avg"), "race_min": d.get("race_min"), "race_laps": d.get("race_laps"),
               "cap": d.get("cap"), "litres": None, "laps": None, "why": ""}
        fpl, avg = d.get("fpl"), d.get("avg")
        if not fpl:
            out["why"] = L("Fais au moins 3 tours propres sur ce circuit pour que je mesure ta conso.",
                           "Do at least 3 clean laps on this track so I can measure your consumption.",
                           "Tüketimini ölçebilmem için bu pistte en az 3 temiz tur at.")
            return out
        if d.get("race_laps"):
            laps = d["race_laps"]
        elif d.get("race_min") and avg:
            laps = math.ceil(d["race_min"] * 60 / avg)
        else:
            out["why"] = L("Indique la durée de la course pour calculer l'essence de départ.",
                           "Enter the race length to work out the starting fuel.",
                           "Başlangıç yakıtını hesaplamak için yarış süresini gir.")
            return out
        litres = math.ceil((laps + 1) * fpl)        # + 1 tour : tour de formation et marge
        cap = d.get("cap")
        stop = bool(cap and litres > cap)
        if stop:
            litres = int(cap)
        out.update({"laps": laps, "litres": litres, "stop": stop,
                    "why": dec(L(f"{laps} tours × {fpl:.2f} L + 1 tour de marge", f"{laps} laps × {fpl:.2f} L + 1 lap margin", f"{laps} tur × {fpl:.2f} L + 1 tur pay")
                               + (L(" — plus que le réservoir : plein, et un arrêt sera nécessaire",
                                    " — more than the tank: full tank, and a stop will be needed",
                                    " — depodan fazla: depo dolu, bir pit stop gerekecek") if stop else ""))})
        return out
