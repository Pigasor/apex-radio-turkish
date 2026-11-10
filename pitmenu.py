"""
Menu des stands de LMU (étape 4) : Jessica règle l'arrêt toute seule.

Lecture  : GET  /rest/garage/PitMenu/receivePitMenu  (liste des lignes du menu)
Écriture : POST /rest/garage/PitMenu/loadPitMenu     (même liste, valeurs modifiées)
Chaque ligne : {"PMC Value": id, "name": "FUEL:", "currentSetting": index, "settings": [{"text": ...}, ...]}
  id 7  = essence (niveau voulu APRÈS l'arrêt, ex. "41l/15 laps")
  id 8  = pneus (0 = "Aucun changement", puis "Neuf Medium", "Neuf Mouillé"...)
  id 2  = réparations
Après écriture on relit le menu pour vérifier que LMU a bien pris les valeurs.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

from i18n import L

BASE = "http://localhost:6397"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FUEL, TIRES, DAMAGE = 7, 8, 2


def _req(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json", "Accept": "application/json"})
    with _OPENER.open(r, timeout=3) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, (json.loads(raw) if raw.strip().startswith(("[", "{")) else raw)


def read_menu() -> list:
    status, j = _req("GET", "/rest/garage/PitMenu/receivePitMenu")
    return j if isinstance(j, list) else []


def _item(menu: list, pmc: int):
    return next((it for it in menu if it.get("PMC Value") == pmc), None)


def _liters(text: str) -> float | None:
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def plan_menu(menu: list, fuel_total: float | None, tyres: str | None, compound: str, repair) -> tuple[list, list]:
    """repair : None = on ne touche pas, False = « Ne pas réparer », "body" = carrosserie, "all" = tout réparer."""
    """Retourne (menu modifié, liste de ce qui change en clair). tyres : None/"keep"/"same"/"Wet"/"dry"."""
    menu = json.loads(json.dumps(menu))   # copie
    changes = []
    it = _item(menu, FUEL)
    if it and fuel_total is not None:
        opts = [(i, _liters(s.get("text"))) for i, s in enumerate(it["settings"])]
        opts = [(i, l) for i, l in opts if l is not None]
        if opts:
            # plus petit niveau qui couvre le besoin, sinon le plein
            ok = [o for o in opts if o[1] >= fuel_total]
            idx, lit = min(ok, key=lambda o: o[1]) if ok else max(opts, key=lambda o: o[1])
            if it["currentSetting"] != idx:
                it["currentSetting"] = idx
            changes.append(L(f"essence à {lit:.0f} litres", f"fuel to {lit:.0f} litres", f"yakıt {lit:.0f} litre"))
    it = _item(menu, TIRES)
    if it and tyres:
        idx = 0
        if tyres != "keep":
            want = None if tyres in ("same", "dry") else tyres
            cur = (compound or "").strip().lower()
            for i, s in enumerate(it["settings"]):
                t, typ = s.get("text", ""), (s.get("type") or "")
                if i == 0 or s.get("isUsed") or "INVALID" in t.upper():
                    continue
                if want == "Wet" and typ == "Wet":
                    idx = i; break
                if tyres == "same" and typ.lower() == cur:
                    idx = i; break
                if tyres == "dry" and typ != "Wet":
                    idx = i; break
            if idx == 0 and tyres == "same":   # composé actuel introuvable : premier pneu sec neuf
                idx = next((i for i, s in enumerate(it["settings"]) if i and not s.get("isUsed") and s.get("type") != "Wet"
                            and "INVALID" not in s.get("text", "").upper()), 0)
        it["currentSetting"] = idx
        typ = (it["settings"][idx].get("type") or "") if idx else ""
        changes.append(L("on garde les pneus", "keep the tyres", "lastikler kalıyor") if idx == 0 else L("pneus pluie neufs", "new wet tyres", "yeni yağmur lastiği") if typ == "Wet"
                       else L(f"pneus neufs {typ.lower()}", f"new {typ.lower()} tyres", f"yeni {typ.lower()} lastik").strip())
    it = _item(menu, DAMAGE)
    if it and repair:
        print(f"  Menu stand, réparations possibles : {[s.get('text') for s in it.get('settings', [])]}")
    if it and repair and len(it["settings"]) > 1:
        opts = [(i, s.get("text", "").lower()) for i, s in enumerate(it["settings"])]
        opts = [(i, t) for i, t in opts
                if ("répar" in t or "repair" in t or "fix" in t) and not any(n in t for n in ("ne pas", "not", "no ", "aucun"))]
        # LMU : « Ne pas réparer », « Réparer la carrosserie », « Tout réparer »
        want_all = repair == "all"
        pick = next((o for o in opts if any(k in o[1] for k in (("tout", "all", "full") if want_all else ("carross", "body", "aero")))), None)
        pick = pick or (opts[-1] if want_all and opts else opts[0] if opts else None)
        if pick:
            it["currentSetting"] = pick[0]
            changes.append(L("on répare tout", "full repairs", "tam onarım") if want_all else L("réparation de la carrosserie", "bodywork repairs", "kaporta onarımı"))
    if it and repair is False and len(it["settings"]) > 1:   # le pilote a dit « pas de réparation »
        idx = next((i for i, s in enumerate(it["settings"])
                    if any(n in s.get("text", "").lower() for n in ("ne pas", "pas de", "not", "no ", "none", "aucun"))), 0)
        it["currentSetting"] = idx
        changes.append(L("pas de réparation", "no repairs", "onarım yok"))
    return menu, changes


class PitCrew:
    """Règle le menu des stands au moment du « box » (mode Auto) ou à la demande."""

    def __init__(self, cfg: dict, demo: bool = False) -> None:
        self.cfg = cfg
        self.demo = demo
        self.mode = cfg.get("pit_auto", "auto")   # "auto" / "demande" / "off"
        self.done_for = None                        # n° d'arrêt déjà préparé
        self.status = ""                            # pour le tableau de bord
        self.busy = False
        self.say = None

    def update(self, s, p) -> None:
        if self.mode != "auto" or not s.on_track or s.in_pits or s.session != "course" or s.game_phase == 8:
            return
        if p.ready and p.stops_needed and p.laps_until_box is not None and p.laps_until_box <= 1:
            if self.done_for != s.pit_stops:
                self.prepare(s, p)

    def prepare(self, s, p, asked: bool = False) -> None:
        if self.busy:
            return
        if not p or p.fuel_to_add is None and not asked:
            return
        self.done_for = s.pit_stops
        fuel_total = getattr(p, "fuel_total", None)
        tyres = "Wet" if p.tyre_compound == "pluie" else "dry" if p.tyre_compound == "slicks" else \
            "same" if p.change_tyres else "keep" if p.change_tyres is False else None
        self.busy = True
        threading.Thread(target=self._run, args=(fuel_total, tyres, s.compound,
                                                       ("all" if getattr(p, "repair_all", False) else "body") if getattr(p, "repair", False)
                                                       else False if getattr(p, "no_repair", False) else None),
                         daemon=True).start()

    def _run(self, fuel_total, tyres, compound, repair) -> None:
        try:
            if self.demo:
                time.sleep(0.5)
                txt = ", ".join(x for x in [L(f"essence à {fuel_total:.0f} litres", f"fuel to {fuel_total:.0f} litres", f"yakıt {fuel_total:.0f} litre") if fuel_total else "",
                                            {"keep": L("on garde les pneus", "keep the tyres", "lastikler kalıyor"), "same": L("pneus neufs", "new tyres", "yeni lastik"),
                                             "Wet": L("pneus pluie", "wet tyres", "yağmur lastiği"), "dry": L("pneus slicks", "slick tyres", "slick lastik")}.get(tyres, ""),
                                            (L("on répare tout", "full repairs", "tam onarım") if repair == "all" else L("réparation de la carrosserie", "bodywork repairs", "kaporta onarımı")) if repair
                                            else L("pas de réparation", "no repairs", "onarım yok") if repair is False else ""] if x)
                self.status = L(f"✓ Réglé (démo) : {txt}", f"✓ Set (demo): {txt}", f"✓ Ayarlandı (demo): {txt}")
                self._say(L(f"Stand réglé : {txt}.", f"Pit menu set: {txt}.", f"Pit menüsü ayarlandı: {txt}."))
                return
            menu = read_menu()
            if not menu:
                raise RuntimeError("menu des stands vide")
            new, changes = plan_menu(menu, fuel_total, tyres, compound, repair)
            status, _ = _req("POST", "/rest/garage/PitMenu/loadPitMenu", new)
            time.sleep(0.4)
            check = read_menu()
            bad = [it["name"] for it in new
                   if (c := _item(check, it["PMC Value"])) and c["currentSetting"] != it["currentSetting"]
                   and it["PMC Value"] in (FUEL, TIRES, DAMAGE)]
            if bad:
                self.status = L("⚠ Menu non pris en compte : ", "⚠ Menu not applied: ", "⚠ Menü uygulanmadı: ") + ", ".join(bad)
                self._say(L("Attention, je n'ai pas réussi à régler tout le menu des stands, vérifie-le.",
                            "Careful, I couldn't set the whole pit menu, check it.",
                            "Dikkat, pit menüsünün tamamını ayarlayamadım, kontrol et."))
                print(f"  Menu stand : écart après écriture sur {bad} (statut {status})")
            else:
                self.status = L("✓ Réglé : ", "✓ Set: ", "✓ Ayarlandı: ") + ", ".join(changes)
                self._say(L("Stand réglé : ", "Pit menu set: ", "Pit menüsü ayarlandı: ") + ", ".join(changes) + ".")
                print(f"  Menu stand réglé : {changes}")
        except Exception as e:
            self.status = L(f"⚠ Impossible de régler le menu ({e.__class__.__name__})", f"⚠ Couldn't set the menu ({e.__class__.__name__})", f"⚠ Menü ayarlanamadı ({e.__class__.__name__})")
            print(f"  Menu stand : erreur {e!r}")
            self._say(L("Je n'arrive pas à régler le menu des stands, fais-le à la main cette fois.",
                        "I can't set the pit menu, do it manually this time.",
                        "Pit menüsünü ayarlayamıyorum, bu sefer elle yap."))
        finally:
            self.busy = False

    def _say(self, text: str) -> None:
        if self.say:
            self.say(None, text, urgent=True)
