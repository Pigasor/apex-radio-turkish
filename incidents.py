"""
Incidents des autres pilotes : sorties de piste et voitures arrêtées.

- Sortie de piste : la voiture est plus loin du centre que le bord de piste (+1,5 m).
- Voiture arrêtée / très lente en piste (< 10 m/s hors des stands) : tête-à-queue, accident.
On n'annonce que ce qui est DEVANT toi (là où tu vas passer dans le demi-tour qui vient),
avec le numéro du virage, et une seule fois par virage toutes les 25 secondes.
"""

from __future__ import annotations

import time

from i18n import L

AHEAD = 0.30         # on annonce seulement ce qui est juste devant toi (30 % de tour)
GLOBAL_GAP = 20.0    # s : au plus une annonce d'incident toutes les 20 s
ON_TRACK = 9.0       # m : la voiture arrêtée doit être SUR la piste (près de la trajectoire) pour être dangereuse
CONFIRM = 1.0        # s : l'incident doit durer un peu (pas un simple passage sur le vibreur)
OFF_LINE = 14.0      # m : vraiment loin de la trajectoire (les dégagements bitumés ne comptent pas sinon)
COOLDOWN = 25.0      # s : pas deux annonces pour le même virage
SLOW = 10.0          # m/s : voiture quasi arrêtée


class Incidents:
    def __init__(self, behavior) -> None:
        self.beh = behavior          # pour retrouver le numéro du virage
        self.since = {}              # id voiture -> (début, type)
        self.said = {}               # virage -> heure de la dernière annonce
        self.active = []             # pour la carte : [{"x","z","kind"}]
        self.say = None

    def _where(self, f: float) -> str:
        c = self.beh.corner_at(f, ahead=0.015) if self.beh else None
        if c is None and self.beh:
            # hors virage : on donne le virage qui suit
            corners = self.beh.map.corners()
            nxt = min(corners, key=lambda k: (k["start"] - f) % 1, default=None)
            return L(f"avant le virage {nxt['id']}", f"before turn {nxt['id']}", f"{nxt['id']}. virajdan önce") if nxt else L("sur la piste", "on track", "pistte")
        return L(f"au virage {c}", f"at turn {c}", f"{c}. virajda") if c else L("sur la piste", "on track", "pistte")

    def update(self, s) -> None:
        if not s.on_track or s.game_phase not in (5, 6) or s.in_pits or s.session == "qualif":
            self.since.clear()
            self.active = []
            return
        now = time.monotonic()
        me = next((c for c in s.cars if c.get("me")), None)
        if me is None or me.get("spd", 99) < 15 or s.game_phase == 6:
            # départ, neutralisation, ou tu es toi-même au ralenti : tout le monde roule doucement
            self.since.clear()
            self.active = []
            return
        active, events = [], []
        for c in s.cars:
            if c.get("me") or c.get("pits") or "off" not in c:
                continue
            off = c["off"]
            if off and self.beh and self.beh.map.saved:
                d = self.beh.map.dist_to_line(c["x"], c["z"], c["f"])
                off = d is not None and d > OFF_LINE
            # seuls les incidents dangereux : voiture arrêtée / très lente SUR la piste
            kind = None
            if c["spd"] < SLOW:
                d = self.beh.map.dist_to_line(c["x"], c["z"], c["f"]) if self.beh and self.beh.map.saved else None
                if d is None or d < ON_TRACK:
                    kind = "arrêt"
            cid = c["id"]
            if kind is None:
                self.since.pop(cid, None)
                continue
            start, k0 = self.since.get(cid, (now, kind))
            if k0 != kind and kind == "arrêt":
                start = now   # une sortie qui se termine à l'arrêt : on repart
            self.since[cid] = (start, kind)
            active.append({"x": c["x"], "z": c["z"], "kind": kind})
            if now - start >= CONFIRM:
                ahead = (c["f"] - me["f"]) % 1
                if ahead < AHEAD:
                    events.append((ahead, kind, c))
        self.active = active
        if not events or not self.say:
            return
        events.sort(key=lambda e: e[0])
        ahead, kind, c = events[0]
        where = self._where(c["f"])
        if now - self.said.get(where, -99) < COOLDOWN or now - self.said.get("*", -99) < GLOBAL_GAP:
            return
        self.said[where] = self.said["*"] = now
        if kind == "arrêt":
            self.say(None, L(f"Attention, voiture arrêtée {where} !", f"Caution, car stopped {where}!", f"Dikkat, {where} duran araç var!"), urgent=True)
        else:
            self.say(None, L(f"Voiture hors piste {where}, attention.", f"Car off {where}, caution.", f"{where} pist dışına çıkan araç var, dikkat."), urgent=True)
