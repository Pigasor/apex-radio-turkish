"""
Alertes radio supplémentaires :
1. Spotter gauche / droite
2. Trafic multi-catégories (plus rapide qui revient, plus lent à doubler)
3. Tendance des écarts (on revient / on se fait reprendre)
4. Limites de piste
5. Prévisions météo (interface locale de LMU)
8. Dégâts après un contact
9. Pneus et freins froids (départ, sortie des stands)
10. Voiture en piste (LMP2 version ELMS ou WEC)
"""

from __future__ import annotations

import json
import math
import random
import sys
import threading
import time
import urllib.request

from i18n import L, dec

L_ = L

# niveau d'encouragements choisi dans l'appli : "discret", "normal" ou "motivant"
ENCOURAGE = "normal"


def set_encourage(level: str) -> None:
    global ENCOURAGE
    ENCOURAGE = level if level in ("discret", "normal", "motivant") else "normal"
   # dans le trafic, « L » sert aussi à la longueur du circuit


def WHEEL_NAME(i: int) -> str:
    return L("avant gauche|avant droite|arrière gauche|arrière droite", "front left|front right|rear left|rear right", "ön sol|ön sağ|arka sol|arka sağ").split("|")[i]


def class_name(c: str) -> str:
    u = c.upper()
    for key, name in (("HYPER", "Hypercar"), ("LMH", "Hypercar"), ("LMDH", "Hypercar"), ("GTE", "GTE"), ("GT3", "GT3"),
                      ("LMP2", "LMP2"), ("LMP3", "LMP3")):
        if key in u:
            return name
    return c or L("voiture", "car", "araç")


def _dix(x: float) -> str:
    d = round(abs(x) * 10)
    if d >= 10:
        return dec(L(f"{abs(x):.1f} seconde", f"{abs(x):.1f} seconds", f"{abs(x):.1f} saniye"))
    return L(f"{max(1, d)} dixième" + ("s" if d > 1 else ""), f"{max(1, d)} tenth" + ("s" if d > 1 else ""), f"onda {max(1, d)} saniye")


# ======================================================================= 1. spotter
class Spotter:
    CAR_LEN = 4.8      # m : chevauchement avant/arrière (vraiment côte à côte)
    SIDE_MIN, SIDE_MAX = 0.8, 4.0
    CLEAR_DELAY = 1.0  # s : voie libre depuis 1 s avant de dire « dégagé »
    CONFIRM = 0.35     # s : la voiture doit rester à côté un instant (pas un dépassement éclair)
    SAME_SIDE_GAP = 4.0  # s : on ne répète pas le même côté trop vite

    def __init__(self, voice) -> None:
        self.voice = voice
        self.enabled = True
        self.hist = []          # dernières positions (t, x, z) pour connaître ta direction
        self.state = "clear"
        self.last_seen = {"left": 0.0, "right": 0.0}
        self.left = self.right = False
        self.first_seen = {"left": None, "right": None}
        self.last_said = {}

    def feed(self, m: dict | None) -> None:
        if not m or not self.enabled or getattr(self, "session", "") == "qualif":
            return   # en qualif tu es seul en piste : pas de spotter
        now = time.monotonic()
        x, z = m["me"]
        self.hist.append((now, x, z))
        self.hist = [h for h in self.hist if now - h[0] < 0.35]
        t0, x0, z0 = self.hist[0]
        dx, dz = x - x0, z - z0
        dist = math.hypot(dx, dz)
        if now - t0 < 0.12 or dist / max(now - t0, 1e-3) < 8:   # à l'arrêt / trop lent : pas de spotter
            self._set(False, False, now, quiet=True)
            return
        fx, fz = dx / dist, dz / dist
        lx, lz = -fz, fx            # gauche (repère LMU : x à gauche, y en haut, z vers l'arrière)
        left = right = False
        for ox, oz in m["others"]:
            ddx, ddz = ox - x, oz - z
            along = ddx * fx + ddz * fz
            side = ddx * lx + ddz * lz
            if abs(along) < self.CAR_LEN and self.SIDE_MIN < abs(side) < self.SIDE_MAX:
                if side > 0:
                    left = True
                else:
                    right = True
        for side, here in (("left", left), ("right", right)):
            if here:
                self.last_seen[side] = now
                if self.first_seen[side] is None:
                    self.first_seen[side] = now
            elif now - self.last_seen[side] >= self.CLEAR_DELAY:
                self.first_seen[side] = None
        # confirmé si présent depuis CONFIRM s ; maintenu tant que pas libre depuis CLEAR_DELAY s
        conf = lambda side: self.first_seen[side] is not None and now - self.first_seen[side] >= self.CONFIRM
        left = conf("left") and (left or now - self.last_seen["left"] < self.CLEAR_DELAY)
        right = conf("right") and (right or now - self.last_seen["right"] < self.CLEAR_DELAY)
        self._set(left, right, now)

    def _set(self, left: bool, right: bool, now: float, quiet: bool = False) -> None:
        state = "both" if left and right else "left" if left else "right" if right else "clear"
        if state != self.state and not quiet:
            msg = {"both": L("Des deux côtés !", "Three wide!", "Üç araç yan yana!"), "left": L("À gauche !", "Car left!", "Solda araç!"),
                   "right": L("À droite !", "Car right!", "Sağda araç!"), "clear": L("Dégagé.", "Clear.", "Temiz.")}[state]
            # pas deux fois la même annonce en quelques secondes ; « dégagé » seulement après une vraie annonce
            if now - self.last_said.get(state, -99) >= self.SAME_SIDE_GAP and (state != "clear" or self.state != "clear"):
                self.last_said[state] = now
                self.voice.say(msg, spot=True)
        self.state, self.left, self.right = state, left, right


# ======================================================================= 2. trafic
class Traffic:
    GLOBAL_GAP = 30.0        # s : pas plus d'une annonce de trafic toutes les 30 s

    CATCH_WINDOW = (8.0, 25.0)  # s : on prévient quand on va rattraper une voiture plus lente dans 8 à 25 s

    def __init__(self, behavior=None) -> None:
        self.beh = behavior
        self.said = {}       # (id, type) -> heure
        self.prev_gap = {}
        self.last = -999.0

    def update(self, s, plan, say) -> None:
        if not s.on_track or s.session != "course" or s.in_pits or not s.track_len:
            return
        me = next((c for c in s.cars if c.get("me")), None)
        if not me:
            return
        my_lap = plan.avg_lap or s.last_lap or s.best_lap
        if not my_lap:
            return
        L = s.track_len
        speed = L / my_lap
        # rythme de chaque catégorie (médiane des meilleurs tours)
        bests = {}
        for c in s.cars:
            if c.get("best"):
                bests.setdefault(c["c"], []).append(c["best"])
        pace = {k: sorted(v)[len(v) // 2] for k, v in bests.items()}
        mine = pace.get(me["c"]) or my_lap
        now = time.monotonic()
        if now - self.last < self.GLOBAL_GAP:
            return
        for c in s.cars:
            if c.get("me") or c.get("pits") or c["c"] == me["c"] or c["c"] not in pace:
                continue
            delta = pace[c["c"]] - mine          # < 0 : catégorie plus rapide
            name = class_name(c["cls"])
            if delta < -0.02 * mine:             # plus rapide : on regarde derrière
                gap = ((me["f"] - c["f"]) % 1) * L / speed
                key = (c["id"], "back")
                prev = self.prev_gap.get(key)
                self.prev_gap[key] = gap
                if 1.0 < gap < 3.0 and prev is not None and prev > gap and now - self.said.get(key, -999) > 90:
                    self.said[key] = self.last = now
                    catch = gap / -delta
                    when = L("elle sera sur toi dans ce tour", "it'll be on you this lap", "bu tur sana yetişir") if catch < 1 else \
                        L(f"elle sera sur toi dans {math.ceil(catch)} tours", f"it'll be on you in {math.ceil(catch)} laps", f"{math.ceil(catch)} tur sonra sana yetişir")
                    say(None, L(f"{name} qui arrive derrière, {gap:.0f} seconde{'s' if round(gap) > 1 else ''}, {when}.",
                                f"{name} coming up behind, {gap:.0f} second{'s' if round(gap) != 1 else ''}, {when}.",
                                f"Arkadan {name} geliyor, {gap:.0f} saniye geride, {when}."))
        self._slower_ahead(s, me, pace, mine, speed, now, say)

    def _slower_ahead(self, s, me, pace, mine, speed, now, say) -> None:
        """Catégorie plus lente devant : on prévient où tu vas la rattraper, pour anticiper le dépassement.
        Une seule annonce à la fois (la plus proche), pas plus d'une fois toutes les 2 minutes par voiture."""
        L = s.track_len
        slow = []
        for c in s.cars:
            if c.get("me") or c.get("pits") or c["c"] == me["c"] or c["c"] not in pace:
                continue
            if pace[c["c"]] - mine <= 0.02 * mine:      # pas nettement plus lente
                continue
            gap = ((c["f"] - me["f"]) % 1) * L / speed  # temps pour arriver là où elle est
            key = (c["id"], "front")
            prev = self.prev_gap.get(key)
            self.prev_gap[key] = gap
            if gap < 8:
                slow.append((gap, prev, c, key))
        slow.sort(key=lambda x: x[0])
        if now - self.last < self.GLOBAL_GAP:
            return
        lo, hi = self.CATCH_WINDOW
        for gap, prev, c, key in slow:
            if prev is None or prev <= gap or now - self.said.get(key, -999) < 120:
                continue
            closing = speed - L / pace[c["c"]]           # m/s gagnés sur elle
            if closing <= 0:
                continue
            t = gap * speed / closing                     # secondes avant de la rattraper
            if not (lo < t < hi):                         # trop tôt (inutile) ou trop tard (déjà dessus)
                continue
            group = [x for x in slow if x[2]["c"] == c["c"] and 0 <= x[0] - gap < 2.0]
            for x in group:
                self.said[x[3]] = now
            self.last = now
            name = class_name(c["cls"])
            corner = self.beh.corner_at((me["f"] + t * speed / L) % 1) if self.beh else None
            where = L_(f", tu la rattrapes au virage {corner}", f", you'll catch it at turn {corner}", f", {corner}. virajda yakalarsın") if corner else ""
            if gap < 1.0:
                sec = L_("moins d'une seconde", "less than a second", "bir saniyeden az")
            else:
                sec = L_(f"{gap:.0f} seconde{'s' if round(gap) > 1 else ''}", f"{gap:.0f} second{'s' if round(gap) != 1 else ''}", f"{gap:.0f} saniye")
            if len(group) > 1:
                where = where.replace("la rattrapes", "les rattrapes").replace("catch it", "catch them")
                say(None, L_(f"{len(group)} {name} devant, à {sec}{where}.", f"{len(group)} {name} cars ahead, {sec}{where}.", f"Önde {len(group)} {name} var, {sec} ileride{where}."))
            else:
                say(None, L_(f"{name} plus lente devant, à {sec}{where}.", f"Slower {name} ahead, {sec}{where}.", f"Önde daha yavaş bir {name} var, {sec} ileride{where}."))
            return


# ======================================================================= 3. écarts
class GapTrend:
    EVERY = 3

    def __init__(self) -> None:
        self.laps = None
        self.ahead, self.behind = [], []
        self.count = 0

    def update(self, s, say) -> None:
        if not s.on_track or s.session != "course":
            return
        if self.laps is None or s.laps_done < self.laps:
            self.laps = s.laps_done
            self.ahead, self.behind = [], []
            return
        if s.laps_done == self.laps:
            return
        self.laps = s.laps_done
        self.count += 1
        pa = (s.ahead or {}).get("place")
        pb = (s.behind or {}).get("place")
        self.ahead.append((pa, s.gap_class_ahead))
        self.behind.append((pb, s.gap_class_behind))
        self.ahead, self.behind = self.ahead[-4:], self.behind[-4:]
        every = {"discret": 5, "normal": self.EVERY, "motivant": 2}[ENCOURAGE]
        if self.count % every != 1 % every or s.in_pits:
            return
        msg = self._ahead_msg() or self._behind_msg()
        msg = msg and msg.replace("PNone", L("la voiture", "the car", "araç"))
        if msg:
            say(None, msg)

    @staticmethod
    def _rate(series):
        if len(series) < 4 or any(g is None for _, g in series) or len({p for p, _ in series}) != 1:
            return None, None, None   # pas assez de tours, ou ce n'est plus la même voiture
        return (series[-1][1] - series[0][1]) / 3, series[-1][1], series[-1][0]

    # phrases d'ingénieur, tirées au hasard pour ne pas répéter toujours la même chose
    @staticmethod
    def _pep(fr: list[str], en: list[str]) -> str:
        if ENCOURAGE == "discret":
            return ""              # juste l'info, sans encouragement
        i = random.randrange(len(fr))
        out = " " + L(fr[i], en[i], PEP_TR.get(en[i]))
        if ENCOURAGE == "motivant" and random.random() < 0.5:
            j = random.randrange(len(MORE_PEP_FR))
            out += " " + L(MORE_PEP_FR[j], MORE_PEP_EN[j], PEP_TR.get(MORE_PEP_EN[j]))
        return out

    def _ahead_msg(self):
        rate, gap, place = self._rate(self.ahead)
        if rate is None or gap > 15:
            return None
        if rate < -0.12:
            if gap < 1.0:
                return L(f"Tu es dans les roues de P{place}, attaque !", f"You're on P{place}'s gearbox, attack!", f"P{place} hemen önünde, saldır!") + self._pep(
                    ["Mets-lui la pression, montre-toi dans ses rétros.", "Il est à portée, cherche l'ouverture au freinage.",
                     "Il commence à craquer, reste collé et attends sa faute."],
                    ["Put him under pressure, show yourself in his mirrors.", "He's in range, look for a gap under braking.",
                     "He's starting to crack, stay close and wait for his mistake."])
            n = math.ceil((gap - 0.8) / -rate)
            return L(f"Tu reprends {_dix(rate)} par tour sur P{place}, dans ses roues dans {n} tour{'s' if n > 1 else ''}.",
                     f"You're gaining {_dix(rate)} a lap on P{place}, on his gearbox in {n} lap{'s' if n > 1 else ''}.",
                     f"P{place} ile farkı tur başına {_dix(rate)} kapatıyorsun, {n} tur sonra dibindesin.") + self._pep(
                    ["Continue comme ça !", "Pousse, il perd du temps !", "Super rythme, va le chercher !",
                     "Il est en difficulté, mets-lui la pression !"],
                    ["Keep it up!", "Push, he's losing time!", "Great pace, go get him!", "He's struggling, put the pressure on!"])
        if rate < -0.05 and gap < 5:
            return L(f"Tu grappilles sur P{place}, petit à petit.", f"You're chipping away at P{place}, bit by bit.", f"P{place} ile farkı yavaş yavaş kapatıyorsun.") + self._pep(
                ["Continue comme ça.", "Reste régulier, ça va payer."], ["Keep it going.", "Stay consistent, it'll pay off."])
        if rate > 0.15:
            return L(f"P{place} s'échappe de {_dix(rate)} par tour.", f"P{place} is pulling away by {_dix(rate)} a lap.", f"P{place} tur başına {_dix(rate)} uzaklaşıyor.") + self._pep(
                ["Accroche-toi, ne le laisse pas filer.", "Reste concentré, fais ta course.", "Pas de panique, la course est longue."],
                ["Hang on, don't let him go.", "Stay focused, run your own race.", "No panic, it's a long race."])
        return None

    def _behind_msg(self):
        rate, gap, place = self._rate(self.behind)
        if rate is None or gap > 15:
            return None
        if rate < -0.12:
            if gap < 1.0:
                return L(f"P{place} est dans tes roues, défends !", f"P{place} is right behind you, defend!", f"P{place} hemen arkanda, savun!") + self._pep(
                    ["Ferme l'intérieur aux freinages.", "Pas d'erreur, soigne tes sorties de virage.", "Ne lui laisse aucune ouverture."],
                    ["Cover the inside into the braking zones.", "No mistakes, nail your corner exits.", "Don't give him any gap."])
            n = math.ceil((gap - 0.8) / -rate)
            return L(f"P{place} revient de {_dix(rate)} par tour, il sera sur toi dans {n} tour{'s' if n > 1 else ''}.",
                     f"P{place} is closing by {_dix(rate)} a lap, he'll be on you in {n} lap{'s' if n > 1 else ''}.",
                     f"P{place} tur başına {_dix(rate)} yaklaşıyor, {n} tur sonra sana yetişir.") + self._pep(
                    ["Il est plus rapide, pousse un peu plus.", "Ne lâche rien, garde ton rythme.",
                     "Reste propre, fais tes trajectoires et il ne passera pas."],
                    ["He's quicker, push a bit harder.", "Don't give up, keep your pace.",
                     "Stay clean, hit your lines and he won't get by."])
        if rate > 0.15:
            return L(f"Tu creuses l'écart sur P{place}, {_dix(rate)} par tour.", f"You're pulling away from P{place}, {_dix(rate)} a lap.", f"P{place} ile farkı açıyorsun, tur başına {_dix(rate)}.") + self._pep(
                ["Super boulot, continue comme ça !", "Tu le décroches, garde ce rythme.", "Parfait, il ne peut pas suivre."],
                ["Great job, keep it up!", "You're dropping him, keep this pace.", "Perfect, he can't keep up."])
        return None


MORE_PEP_FR = ["Allez, allez !", "On y croit !", "Tu es en feu aujourd'hui !", "Superbe pilotage !", "On lâche rien !",
               "C'est exactement ce qu'il faut faire.", "Bravo, c'est du beau travail."]
MORE_PEP_EN = ["Come on, come on!", "We believe in it!", "You're on fire today!", "Superb driving!", "Never give up!",
               "That's exactly what we need.", "Proud of you."]
# Turc : les phrases d'encouragement, rangées par leur version anglaise (absente = anglais).
PEP_TR = {
    "Come on, come on!": "Hadi, hadi!", "We believe in it!": "Buna inanıyoruz!", "You're on fire today!": "Bugün alev alevsin!",
    "Superb driving!": "Harika sürüş!", "Never give up!": "Asla pes etme!",
    "That's exactly what we need.": "Tam olarak ihtiyacımız olan bu.", "Proud of you.": "Seninle gurur duyuyorum.",
    "Put him under pressure, show yourself in his mirrors.": "Baskı kur, aynalarında görün.",
    "He's in range, look for a gap under braking.": "Menzilde, frenajda boşluk kolla.",
    "He's starting to crack, stay close and wait for his mistake.": "Çözülmeye başladı, yakın kal ve hatasını bekle.",
    "Keep it up!": "Böyle devam!", "Push, he's losing time!": "Bastır, zaman kaybediyor!",
    "Great pace, go get him!": "Harika tempo, yakala onu!", "He's struggling, put the pressure on!": "Zorlanıyor, baskıyı artır!",
    "Keep it going.": "Böyle devam.", "Stay consistent, it'll pay off.": "İstikrarlı kal, karşılığını alacaksın.",
    "Hang on, don't let him go.": "Dayan, kaçmasına izin verme.", "Stay focused, run your own race.": "Odaklan, kendi yarışını sür.",
    "No panic, it's a long race.": "Panik yok, yarış uzun.",
    "Cover the inside into the braking zones.": "Frenajlarda iç çizgiyi kapat.",
    "No mistakes, nail your corner exits.": "Hata yok, viraj çıkışlarına özen göster.",
    "Don't give him any gap.": "Ona hiç boşluk bırakma.",
    "He's quicker, push a bit harder.": "O daha hızlı, biraz daha bastır.", "Don't give up, keep your pace.": "Pes etme, tempoyu koru.",
    "Stay clean, hit your lines and he won't get by.": "Temiz kal, çizgini koru, geçemez.",
    "Great job, keep it up!": "Harika iş, böyle devam!", "You're dropping him, keep this pace.": "Onu koparıyorsun, bu tempoyu koru.",
    "Perfect, he can't keep up.": "Mükemmel, sana ayak uyduramıyor.",
}


# ======================================================================= 11. places gagnées / perdues
class Places:
    """Après le 1er tour : « Bon départ, tu as gagné 3 places ! ». Puis pendant la course,
    à chaque place gagnée ou perdue (sauf autour des arrêts aux stands, où ça bouge pour rien)."""
    STABLE = 4.0       # s : la place doit tenir quelques secondes (pas un croisement éclair)
    COOLDOWN = 25.0    # s entre deux annonces
    PIT_CALM = 90.0    # s sans annonce après un passage aux stands

    def __init__(self) -> None:
        self.key = None
        self.start = None        # place au départ
        self.told_start = False
        self.last_said = None    # dernière place annoncée
        self.cand, self.cand_t = None, 0.0
        self.last_t = -1e9
        self.pit_t = -1e9

    def update(self, s, say) -> None:
        if not s.on_track or s.session != "course" or s.done:
            return
        pos = s.class_position or s.position
        if not pos:
            return
        key = (s.track, s.session_code)
        if key != self.key:
            self.__init__()
            self.key = key
        if s.in_pits:
            self.pit_t = s.elapsed
        if self.start is None:
            if s.laps_done == 0:
                self.start = pos
            return
        if not self.told_start:
            if s.laps_done >= 1:
                self.told_start = True
                self.last_said, self.last_t = pos, s.elapsed
                self._start_msg(pos, say)
            return
        if ENCOURAGE == "discret":
            return
        if s.in_pits or s.elapsed - self.pit_t < self.PIT_CALM:
            self.last_said = pos
            return
        if pos != self.cand:
            self.cand, self.cand_t = pos, s.elapsed
            return
        if pos == self.last_said or s.elapsed - self.cand_t < self.STABLE or s.elapsed - self.last_t < self.COOLDOWN:
            return
        diff = self.last_said - pos
        self.last_said, self.last_t = pos, s.elapsed
        if abs(diff) > 3:
            return                 # gros saut : arrêts des autres, pénalité… pas un vrai dépassement
        if diff > 0:
            n = L(f"{diff} places de plus" if diff > 1 else "Une place de plus", f"{diff} places gained" if diff > 1 else "One place up", f"{diff} sıra kazandın" if diff > 1 else "Bir sıra yükseldin")
            pep = random.choice([L("Bien joué !", "Nice one!", "Güzel!"), L("Superbe dépassement !", "Great move!", "Harika hamle!"), L("Continue !", "Keep going!", "Devam et!")])
            if ENCOURAGE == "motivant":
                j = random.randrange(len(MORE_PEP_FR)); pep += " " + L(MORE_PEP_FR[j], MORE_PEP_EN[j], PEP_TR.get(MORE_PEP_EN[j]))
            say(None, L(f"{n}, tu es P{pos}. ", f"{n}, you're P{pos}. ", f"{n}, P{pos} oldun. ") + pep)
        else:
            k = -diff
            say(None, L(f"Tu perds {k if k > 1 else 'une'} place{'s' if k > 1 else ''}, P{pos}. ", f"You lose {k} place{'s' if k > 1 else ''}, P{pos}. ", f"{k} sıra kaybettin, P{pos}. ")
                + random.choice([L("Accroche-toi, on va la reprendre.", "Hang on, we'll get it back.", "Dayan, geri alacağız."),
                                 L("Pas grave, reste concentré.", "No worries, stay focused.", "Sorun değil, odaklan."),
                                 L("Respire, la course est longue.", "Breathe, it's a long race.", "Nefes al, yarış uzun.")]))

    def _start_msg(self, pos, say) -> None:
        d = self.start - pos
        disc = ENCOURAGE == "discret"
        if d > 0:
            msg = L(f"Bon départ, tu as gagné {d if d > 1 else 'une'} place{'s' if d > 1 else ''}, tu es P{pos}.",
                    f"Good start, you gained {d} place{'s' if d > 1 else ''}, you're P{pos}.",
                    f"İyi start, {d} sıra kazandın, P{pos} oldun.")
            if not disc:
                msg += " " + random.choice([L("Superbe !", "Superb!", "Muhteşem!"), L("Continue comme ça !", "Keep it up!", "Böyle devam!"),
                                            L("Excellent premier tour !", "Excellent first lap!", "Mükemmel bir ilk tur!")])
        elif d < 0:
            msg = L(f"Premier tour difficile, tu as perdu {-d if d < -1 else 'une'} place{'s' if d < -1 else ''}, tu es P{pos}.",
                    f"Tough first lap, you lost {-d} place{'s' if d < -1 else ''}, you're P{pos}.",
                    f"Zor bir ilk tur, {-d} sıra kaybettin, P{pos} oldun.")
            if not disc:
                msg += " " + random.choice([L("Pas grave, on va les reprendre.", "No worries, we'll get them back.", "Sorun değil, geri alacağız."),
                                            L("La course est longue, reste calme.", "It's a long race, stay calm.", "Yarış uzun, sakin kal.")])
        else:
            msg = L(f"Premier tour propre, tu gardes ta place, P{pos}.", f"Clean first lap, you hold your place, P{pos}.", f"Temiz bir ilk tur, yerini korudun, P{pos}.")
            if not disc:
                msg += " " + L("Maintenant, on attaque !", "Now let's attack!", "Şimdi saldırı zamanı!")
        say(None, msg)


# ======================================================================= 12. moral : encouragements au fil des tours
class Morale:
    """Encouragements qui ne dépendent pas des adversaires : utile quand on est seul, loin devant
    ou loin derrière (après un accident par exemple). Normal : rare. Motivant : environ un tour sur deux."""

    def __init__(self) -> None:
        self.key = None
        self.lap = 0
        self.prev = None          # tour précédent (s)
        self.since = 0            # tours depuis le dernier encouragement
        self.trouble_lap = None   # tour du dernier incident (dégâts, place perdue en masse)
        self.start_pos = None

    def note_trouble(self, s) -> None:
        self.trouble_lap = s.laps_done

    def update(self, s, say) -> None:
        if ENCOURAGE == "discret" or not s.on_track or s.session != "course" or s.done:
            return
        key = (s.track, s.session_code)
        if key != self.key:
            self.__init__(); self.key = key
        if s.laps_done == self.lap:
            return
        self.lap = s.laps_done
        last, prev = s.last_lap, self.prev
        self.prev = last
        pos_now = s.class_position or s.position
        if pos_now and self.start_pos and pos_now - self.start_pos >= 3:
            self.trouble_lap = s.laps_done        # on a perdu beaucoup de places d'un coup (accident, tête-à-queue…)
        self.start_pos = pos_now
        if s.laps_done < 2 or s.in_pits or not last:
            return
        self.since += 1
        long_lap = last > 150          # Le Mans, Spa… : un tour dure 2 à 4 minutes, on parle plus souvent
        every = (1 if long_lap else 2) if ENCOURAGE == "motivant" else (3 if long_lap else 5)
        if self.since < every:
            return
        self.since = 0
        pos = s.class_position or s.position
        lines = []
        if prev and last < prev - 0.3:
            d = prev - last
            lines.append(L(f"Super tour, {d:.1f} seconde de mieux que le précédent !".replace(".", ","),
                           f"Great lap, {d:.1f} seconds quicker than the last one!",
                           f"Harika tur, öncekinden {d:.1f} saniye daha hızlı!"))
        elif prev and abs(last - prev) < 0.4:
            lines.append(L("Belle régularité, continue sur ce rythme.", "Nice and consistent, keep this pace.", "Güzel ve istikrarlı, bu tempoyu koru."))
        if self.trouble_lap is not None and s.laps_done - self.trouble_lap <= 6:
            lines.append(random.choice([
                L("On a eu un coup dur, mais tu remontes. Continue comme ça !", "We had a tough moment, but you're coming back. Keep it up!", "Zor bir an yaşadık ama toparlanıyorsun. Böyle devam!"),
                L("Oublie l'incident, regarde devant, tu vas plus vite qu'eux.", "Forget the incident, look ahead, you're quicker than them.", "Olayı unut, ileri bak, onlardan daha hızlısın."),
                L("Belle réaction après le pépin, je sens que tu vas remonter.", "Great reaction after the trouble, I can feel you climbing back.", "Sorundan sonra harika toparladın, yeniden yükseldiğini hissediyorum.")]))
        if pos and pos <= 3:
            lines.append(L(f"P{pos}, tu es sur le podium, garde la tête froide !", f"P{pos}, you're on the podium, stay cool!", f"P{pos}, podyumdasın, sakin kal!"))
        if not lines:
            lines = [L("Tu fais du bon boulot, reste concentré.", "You're doing a good job, stay focused.", "İyi iş çıkarıyorsun, odaklan."),
                     L("Chaque tour compte, on continue à pousser.", "Every lap counts, keep pushing.", "Her tur önemli, bastırmaya devam."),
                     L("La course est longue, tout peut encore arriver.", "It's a long race, anything can still happen.", "Yarış uzun, her şey olabilir."),
                     L("Bien, propre et régulier, c'est comme ça qu'on gagne des places.",
                       "Good, clean and consistent, that's how we gain places.",
                       "Güzel, temiz ve istikrarlı; sıralar böyle kazanılır.")]
        fresh = [l for l in lines if l != getattr(self, "last_msg", None)] or lines
        msg = random.choice(fresh)
        self.last_msg = msg
        if ENCOURAGE == "motivant" and random.random() < 0.4:
            j = random.randrange(len(MORE_PEP_FR)); msg += " " + L(MORE_PEP_FR[j], MORE_PEP_EN[j], PEP_TR.get(MORE_PEP_EN[j]))
        say(None, msg)


# ======================================================================= 4. limites de piste
class TrackLimits:
    def __init__(self, behavior) -> None:
        self.beh = behavior
        self.steps = None

    def update(self, s, say) -> None:
        if not s.on_track:
            return
        if self.steps is None or s.tl_steps < self.steps:
            self.steps = s.tl_steps
            return
        if s.tl_steps > self.steps:
            self.steps = s.tl_steps
            me = next((c for c in s.cars if c.get("me")), None)
            c = self.beh.corner_at(me["f"], ahead=0.01) if me and self.beh else None
            where = L(f" au virage {c}", f" at turn {c}", f" {c}. virajda") if c else ""
            if s.tl_per_penalty:
                left = s.tl_per_penalty - s.tl_steps
                if left > 3:
                    say(None, L(f"Limites de piste{where}, avertissement {s.tl_steps} sur {s.tl_per_penalty}.",
                                f"Track limits{where}, warning {s.tl_steps} of {s.tl_per_penalty}.",
                                f"Pist sınırı ihlali{where}, {s.tl_per_penalty} haktan {s.tl_steps}. uyarı."))
                    return
                tail = L("la prochaine, c'est la pénalité !", "next one is a penalty!", "bir sonraki ceza!") if left <= 1 else \
                    L(f"encore {left} avant la pénalité.", f"{left} more before a penalty.", f"cezaya {left} uyarı kaldı.")
                say(None, L(f"Limites de piste{where}, avertissement {s.tl_steps} sur {s.tl_per_penalty}, {tail}",
                            f"Track limits{where}, warning {s.tl_steps} of {s.tl_per_penalty}, {tail}",
                            f"Pist sınırı ihlali{where}, {s.tl_per_penalty} haktan {s.tl_steps}. uyarı, {tail}"), urgent=left <= 1)
            else:
                say(None, L(f"Limites de piste{where}, avertissement numéro {s.tl_steps}.", f"Track limits{where}, warning number {s.tl_steps}.", f"Pist sınırı ihlali{where}, {s.tl_steps}. uyarı."))


# ======================================================================= 9. pneus et freins froids
CARBON = ("HYPER", "LMH", "LMDH", "LMP2", "GTE")   # freins carbone : ils chauffent beaucoup plus


class WarmUp:
    """Au départ de la course et à chaque sortie des stands : « pneus froids, freins froids »,
    puis « tout est en température, tu peux attaquer »."""
    TIMEOUT = 300.0      # s : si ça ne chauffe jamais (pluie, rythme lent), on n'insiste pas

    def __init__(self) -> None:
        self.key = None          # (circuit, session) : un seul départ par séance
        self.was_in_pits = None
        self.watch = None        # moment (temps de séance) où on surveille
        self.warned = False
        self.brake_peak = 0.0

    @staticmethod
    def limits(s) -> tuple[float, float]:
        opt = s.tyre_opt if 40 <= (s.tyre_opt or 0) <= 150 else 80.0
        c = (s.car_class or "").upper()
        brake = 250.0 if any(k in c for k in CARBON) else 180.0
        return opt - 15.0, brake

    def cold(self, s) -> tuple[bool, bool]:
        tyre_ok, brake_ok = self.limits(s)
        tyres = [t for t in s.tyre_temp if t > -50]
        brakes = [b for b in s.brake_temp[:2] if b > -50]   # freins avant : ceux qui comptent au premier virage
        # les freins refroidissent vite dans les lignes droites : on retient le pic atteint depuis le départ
        if brakes:
            self.brake_peak = max(self.brake_peak, sum(brakes) / len(brakes))
        tc = bool(tyres) and sum(tyres) / len(tyres) < tyre_ok
        bc = bool(brakes) and self.brake_peak < brake_ok
        return tc, bc

    def start(self, s) -> None:
        self.watch, self.warned, self.brake_peak = s.elapsed, False, 0.0

    def update(self, s, say) -> None:
        if not s.on_track or s.done:
            self.was_in_pits = None
            return
        key = (s.track, s.session_code)
        # départ de la course : tour de formation ou feu vert, au 1er tour
        if s.session == "course" and key != self.key and s.game_phase in (3, 4, 5) and s.laps_done == 0 and not s.in_pits:
            self.key = key
            self.start(s)
        # sortie des stands (ou du garage), dans n'importe quelle séance
        if self.was_in_pits and not s.in_pits:
            self.start(s)
        self.was_in_pits = s.in_pits
        if self.watch is None:
            return
        if s.elapsed - self.watch > self.TIMEOUT or s.elapsed < self.watch:
            self.watch = None
            return
        tc, bc = self.cold(s)
        if not self.warned:
            self.warned = True
            if tc and bc:
                say(None, L("Attention, pneus froids et freins froids. Doucement au premier freinage.",
                                "Careful, cold tyres and cold brakes. Easy on the first braking zone.",
                                "Dikkat, lastikler ve frenler soğuk. İlk frenajda yavaş."))
            elif tc:
                say(None, L("Attention, pneus froids. Fais-les monter en température avant d'attaquer.",
                                "Careful, cold tyres. Bring them up to temperature before you push.",
                                "Dikkat, lastikler soğuk. Bastırmadan önce ısıt."))
            elif bc:
                say(None, L("Attention, freins froids. Freine plus tôt les premiers virages.",
                                "Careful, cold brakes. Brake earlier for the first few corners.",
                                "Dikkat, frenler soğuk. İlk birkaç virajda erken frenle."))
            else:
                self.watch = None    # déjà chaud : rien à dire
            return
        if not tc and not bc:
            self.watch = None
            say(None, L("Pneus et freins en température. Vas-y, tu peux attaquer !",
                            "Tyres and brakes are up to temperature. Go on, you can push now!",
                            "Lastikler ve frenler ısındı. Hadi, artık bastırabilirsin!"))


# ======================================================================= 10. voiture en piste
def car_label(car_class: str, car: str = "") -> str:
    """« LMP2, version ELMS » : la LMP2 existe en deux versions dans LMU."""
    from setups import series_tag
    name = class_name(car_class)
    t = series_tag(car_class, car)
    return L(f"{name}, version {t}", f"{name}, {t} version", f"{name}, {t} versiyonu") if t else name


def car_sentence(car_class: str, car: str = "") -> str:
    if not car_class:
        return L("Je ne vois pas encore ta voiture. Va au garage ou monte dans la voiture.",
                 "I can't see your car yet. Go to the garage or get in the car.",
                 "Aracını henüz göremiyorum. Garaja git veya araca bin.")
    from setups import series_tag
    out = L(f"Tu es en {car_label(car_class, car)}.", f"You're in the {car_label(car_class, car)}.", f"Aracın: {car_label(car_class, car)}.")
    t = series_tag(car_class, car)
    if t:
        out += L(f" Charge bien un setup fait pour la {class_name(car_class)} {t}.",
                 f" Make sure you load a setup made for the {t} {class_name(car_class)}.",
                 f" {t} {class_name(car_class)} için hazırlanmış bir setup yüklediğinden emin ol.")
    return out


class CarInfo:
    """Dès que tu prends une voiture (au garage, avant de choisir le setup), Apex dit laquelle."""

    def __init__(self) -> None:
        self.key = None

    def update(self, s, say) -> None:
        if not s.connected or not s.car_class:
            return
        key = (s.track, s.car_class, s.car)
        if key == self.key:
            return
        first = self.key is None or self.key[:2] != key[:2]
        self.key = key
        if first:
            say(None, car_sentence(s.car_class, s.car))


# ======================================================================= 8. dégâts
class Damage:
    def __init__(self) -> None:
        self.dents = None
        self.flat = [False] * 4
        self.wheel_off = [False] * 4
        self.part_off = False
        self.wear = None           # dégâts lus dans l'interface de LMU (aéro, suspensions)
        self.repair = False
        self.choice = None         # choix du pilote à la radio : True / False / None (= Apex décide)
        self.all = False           # mécanique touchée : tout réparer
        self.choice_all = True     # « répare » à la radio : tout, sauf s'il dit « la carrosserie »
        self._fetching = False
        self._last_fetch = 0.0
        self._session = None

    def update(self, s, say) -> None:
        if not s.on_track:
            return
        key = (s.track, s.session_code)
        if key != self._session:          # nouvelle session : on repart d'une voiture neuve
            self._session = key
            self.repair, self.choice, self.wear, self.dents, self.all = False, None, None, None, False
        me = next((c for c in s.cars if c.get("me")), None)
        moving = me is not None and me.get("spd", 0) > 5 and not s.in_pits
        if self.dents is None or not moving:
            # au garage / au stand / voiture remise en piste : on prend les valeurs sans rien annoncer
            self.dents, self.flat, self.wheel_off, self.part_off = list(s.dents), list(s.flat), list(s.wheel_off), s.part_off
            if s.in_pits and s.pit_state == 3:
                self.repair, self.choice, self.all = False, None, False
            self._apply(s)
            return
        # carrosserie
        if sum(s.dents) > sum(self.dents):
            worst = max(s.dents)
            say(None, L("Contact ! ", "Contact! ", "Temas! ")
                + (L("Gros dégâts à la carrosserie.", "Heavy body damage.", "Kaportada ağır hasar.") if worst >= 2 else L("Dégâts légers à la carrosserie.", "Light body damage.", "Kaportada hafif hasar."))
                + L(" Je regarde l'aéro et les suspensions.", " Checking aero and suspension.", " Aero ve süspansiyonu kontrol ediyorum."), urgent=True)
            if worst >= 2:
                self.repair = True
            self._last_fetch = 0   # on relit l'interface de LMU tout de suite
        self.dents = list(s.dents)
        # crevaisons, roues
        for i in range(4):
            if s.flat[i] and not self.flat[i]:
                say(None, L(f"Crevaison {WHEEL_NAME(i)} ! Rentre au stand, doucement.", f"Puncture {WHEEL_NAME(i)}! Box, take it easy.", f"{WHEEL_NAME(i).capitalize()} lastik patladı! Pite gir, yavaş."), urgent=True)
                self.repair = self.all = True
            if s.wheel_off[i] and not self.wheel_off[i]:
                say(None, L(f"Roue {WHEEL_NAME(i)} perdue ! Rentre au stand.", f"Lost the {WHEEL_NAME(i)} wheel! Box now.", f"{WHEEL_NAME(i).capitalize()} tekerlek koptu! Hemen pite gir."), urgent=True)
                self.repair = self.all = True
        self.flat, self.wheel_off = list(s.flat), list(s.wheel_off)
        if s.part_off and not self.part_off:
            say(None, L("Une pièce s'est détachée de la voiture.", "A part has come off the car.", "Araçtan bir parça koptu."), urgent=True)
        self.part_off = s.part_off
        if s.in_pits and s.pit_state == 3:
            self.repair, self.choice, self.all = False, None, False   # arrêté au stand : on considère que c'est réglé
        self._poll_api(say)
        self._apply(s)

    def _apply(self, s) -> None:
        # plus aucun dégât lisible (voiture réparée ou remise à neuf) : plus rien à réparer
        if self.repair and self.wear is not None and not any(s.flat) and not any(s.wheel_off) and max(s.dents or [0]) == 0:
            aero = self.wear.get("aero") or 0
            susp = [v for v in (self.wear.get("susp") or []) if isinstance(v, (int, float))]
            if aero < 0.05 and all(v < 0.05 for v in susp):
                self.repair = self.all = False
        s.repair = self.repair if self.choice is None else self.choice
        s.no_repair = self.choice is False
        s.repair_all = s.repair and (self.choice_all if self.choice is True else self.all)

    def _poll_api(self, say) -> None:
        if sys.platform != "win32" or self._fetching or time.monotonic() - self._last_fetch < 20:
            return
        self._last_fetch = time.monotonic()
        self._fetching = True
        threading.Thread(target=self._fetch, args=(say,), daemon=True).start()

    def _fetch(self, say) -> None:
        try:
            j = lmu_get("/rest/garage/UIScreen/RepairAndRefuel")
            w = (j or {}).get("wearables") or {}
            aero = (w.get("body") or {}).get("aero")
            if aero is not None:
                aero = max(0.0, min(1.0, float(aero)))   # LMU peut dépasser 100 % après un gros choc
            susp = w.get("suspension") or []
            new = {"aero": aero, "susp": susp[:4]}
            old = self.wear
            self.wear = new
            if old is None:
                return
            if aero is not None and (old.get("aero") or 0) + 0.05 < aero:
                say(None, L(f"Aéro endommagé à {aero * 100:.0f} %.", f"Aero damaged {aero * 100:.0f} %.", f"Aero hasarı %{aero * 100:.0f}.")
                    + (L(" On répare au prochain arrêt.", " We'll fix it at the next stop.", " Bir sonraki pit stopta onarırız.") if aero > 0.15 else ""))
                if aero > 0.15:
                    self.repair = True
            for i, v in enumerate(susp[:4]):
                o = (old.get("susp") or [0, 0, 0, 0])[i] if old.get("susp") else 0
                if isinstance(v, (int, float)) and v > (o or 0) + 0.05:
                    say(None, L(f"Suspension {WHEEL_NAME(i)} touchée à {v * 100:.0f} %. Attention au comportement.",
                                f"{WHEEL_NAME(i).capitalize()} suspension damaged {v * 100:.0f} %. Watch the handling.",
                                f"{WHEEL_NAME(i).capitalize()} süspansiyon hasarı %{v * 100:.0f}. Aracın davranışına dikkat."))
                    if v > 0.15:
                        self.repair = self.all = True
        except Exception:
            pass
        finally:
            self._fetching = False


# ======================================================================= 5. météo
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def lmu_get(path: str):
    with _OPENER.open("http://localhost:6397" + path, timeout=2) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


SKY_FR = ["dégagé", "quelques nuages", "nuageux", "couvert", "très couvert", "bruine", "pluie légère",
          "pluie", "forte pluie", "orage", "déluge"]
SKY_EN = ["clear", "few clouds", "cloudy", "overcast", "heavy overcast", "drizzle", "light rain",
          "rain", "heavy rain", "storm", "downpour"]


class Weather:
    NODES = ["START", "NODE_25", "NODE_50", "NODE_75", "FINISH"]

    def __init__(self, demo: bool = False) -> None:
        self.demo = demo
        self.forecast = []          # [{"at": 0..1, "rain": 0..1, "sky": int, "temp": °C}]
        self.said = set()
        self._fetching = False
        self._last = 0.0

    def update(self, s, say) -> None:
        if not s.on_track:
            return
        if self.demo and not self.forecast:
            self.forecast = [{"at": 0.0, "rain": 0.0, "sky": 1, "temp": 21}, {"at": 0.25, "rain": 0.3, "sky": 3, "temp": 20},
                             {"at": 0.5, "rain": 0.8, "sky": 7, "temp": 18}, {"at": 0.75, "rain": 0.4, "sky": 4, "temp": 18},
                             {"at": 1.0, "rain": 0.1, "sky": 2, "temp": 19}]
        elif not self.demo and not self._fetching and time.monotonic() - self._last > 60 and sys.platform == "win32":
            self._last = time.monotonic()
            self._fetching = True
            threading.Thread(target=self._fetch, args=(s.session_code,), daemon=True).start()
        self._announce(s, say)

    def _fetch(self, code: int) -> None:
        try:
            # 1) prévisions de la session en cours (écran Réparation/Ravitaillement de LMU)
            try:
                nodes = ((lmu_get("/rest/garage/UIScreen/RepairAndRefuel") or {}).get("weatherForecast") or {}).get("nodes") or {}
                rain, sky, temp = nodes.get("RainChance") or [], nodes.get("Sky") or [], nodes.get("Temperature") or []
                if rain and sky:
                    n = min(len(rain), len(sky))
                    self.forecast = [{"at": i / max(1, n - 1), "rain": (rain[i] or 0) / 100, "sky": int(sky[i] or 0),
                                      "temp": temp[i] if i < len(temp) else None} for i in range(n)]
                    return
            except Exception:
                pass
            # 2) sinon, prévisions générales par type de session
            key = "PRACTICE" if code <= 4 else "QUALIFY" if code <= 8 else "RACE"
            j = lmu_get("/rest/sessions/weather") or {}
            sess = j.get(key) or {}
            out = []
            for i, name in enumerate(self.NODES):
                n = sess.get(name)
                if not n:
                    continue
                val = lambda k: (n.get(k) or {}).get("currentValue")
                out.append({"at": i / 4, "rain": (val("WNV_RAIN_CHANCE") or 0) / 100, "sky": int(val("WNV_SKY") or 0),
                            "temp": val("WNV_TEMPERATURE")})
            if out:
                self.forecast = out
        except Exception:
            pass
        finally:
            self._fetching = False

    def _announce(self, s, say) -> None:
        if not self.forecast or s.time_left is None:
            return
        total = s.elapsed + s.time_left
        if total <= 0:
            return
        now = s.elapsed / total
        raining = s.raining > 0.1
        for n in self.forecast:
            mins = (n["at"] - now) * total / 60
            if not (2 <= mins <= 20):
                continue
            key = (round(n["at"], 2), "rain" if n["rain"] >= 0.5 else "dry")
            if key in self.said:
                continue
            if not raining and n["rain"] >= 0.5:
                self.said.add(key)
                say(None, L(f"Météo : pluie probable dans environ {mins:.0f} minutes, {n['rain'] * 100:.0f} % de risque.",
                            f"Weather: rain likely in about {mins:.0f} minutes, {n['rain'] * 100:.0f} % chance.",
                            f"Hava durumu: yaklaşık {mins:.0f} dakika sonra yağmur bekleniyor, olasılık %{n['rain'] * 100:.0f}."))
            elif raining and n["rain"] < 0.2 and n["sky"] <= 3:
                self.said.add(key)
                say(None, L(f"Météo : éclaircie prévue dans environ {mins:.0f} minutes.", f"Weather: drying up in about {mins:.0f} minutes.", f"Hava durumu: yaklaşık {mins:.0f} dakika sonra yağmur diniyor."))

    def ui(self, s) -> list:
        if not self.forecast or s.time_left is None:
            return []
        total = s.elapsed + s.time_left
        out = []
        for n in self.forecast:
            mins = (n["at"] * total - s.elapsed) / 60
            if mins >= -1:
                out.append({"mins": round(mins), "rain": round(n["rain"] * 100), "sky": L("|".join(SKY_FR), "|".join(SKY_EN), "açık|az bulutlu|bulutlu|kapalı|çok kapalı|çisenti|hafif yağmur|yağmur|şiddetli yağmur|fırtına|sağanak").split("|")[max(0, min(10, n["sky"]))]})
        return out[:4]
