"""
Ingénieur de course pour Le Mans Ultimate — étape 1 : tableau de bord stratégie.

Lancement :
    python app.py          -> lit le jeu en direct
    python app.py --demo   -> course simulée pour tester sans le jeu

Le tableau de bord s'ouvre dans le navigateur (http://localhost:8765).
On peut aussi l'ouvrir sur un téléphone ou une tablette du même réseau
avec l'adresse affichée au démarrage.
"""

from __future__ import annotations

import argparse
import os
import json
import socket
import sys
import threading
import time
import traceback
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from strategy import Engineer
from telemetry import DemoSource, Snapshot
import voice as voices
from voice import Voice
import questions
import i18n
import updates
import desktop
from i18n import L
from questions import answer
from laptimes import LapTimes
from trackmap import TrackMap
from behavior import Behavior
from setups import Workshop
from incidents import Incidents
from lmu_probe import Probe
from alerts import Spotter, Traffic, GapTrend, TrackLimits, Damage, Weather, WarmUp, CarInfo, Places, Morale, car_label, set_encourage
import debrief
from pitmenu import PitCrew
from radio import save_config
from radio import Radio
from tactics import Penalties, Undercut, Pace, FuelBook

HERE = Path(__file__).parent
from paths import DATA, LOCAL, MIGRATED  # noqa: E402
PORT = 8765


def system_lang() -> str:
    """Premier lancement : français ou turc si Windows est dans cette langue, sinon anglais."""
    try:
        if sys.platform == "win32":
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return {0x0C: "fr", 0x1F: "tr"}.get(lid & 0x3FF, "en")
        import locale
        loc = (locale.getlocale()[0] or "").lower()[:2]
        return loc if loc in ("fr", "tr") else "en"
    except Exception:
        return "fr"


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.payload = b"{}"
        self.snap = Snapshot()
        self.plan = None
        self.eng = None
        self.map = TrackMap()
        self.radio = Radio(self.on_question, self.on_shortcut)
        i18n.set_lang(self.radio.cfg.get("lang") or system_lang())   # avant la voix : elle charge le modèle de la bonne langue
        voices.CHOICE.update(self.radio.cfg.get("voices") or {})      # voix choisie pour chaque langue
        self.voice = Voice()
        self.workshop = None
        self.beh = None
        self.demo = False
        self.last_track = ""
        self.fuel = FuelBook()

    def track(self) -> str:
        return self.snap.track or self.last_track

    # ------------------------------------------------ boutons du tableau de bord et raccourcis du volant
    def toggle_voice(self) -> None:
        v = self.voice
        v.enabled = not v.enabled
        if v.enabled:
            v.say(L("Radio active.", "Radio on.", "Telsiz açık."), urgent=True)
        print(f"  Radio : {'activée' if v.enabled else 'coupée'}")

    def toggle_quiet(self) -> None:
        v = self.voice
        v.quiet = not v.quiet
        v.say(L("Mode silence : je ne te dis plus que l'urgent.", "Quiet mode: urgent calls only.", "Sessiz mod: yalnızca acil çağrılar.") if v.quiet
              else L("Fin du silence, je te redonne toutes les infos.", "Quiet mode off, back to full info.", "Sessiz mod kapalı, tüm bilgilere dönüyorum."),
              urgent=True)

    def toggle_spotter(self) -> None:
        sp = getattr(self, "spotter", None)
        if sp:
            sp.enabled = not sp.enabled
            self.radio.cfg["spotter"] = sp.enabled
            save_config(self.radio.cfg)
            self.voice.say(L("Spotter activé.", "Spotter on.", "Spotter açık.") if sp.enabled else L("Spotter coupé.", "Spotter off.", "Spotter kapalı."),
                           urgent=True)

    def on_shortcut(self, name: str) -> None:
        {"spotter": self.toggle_spotter, "radio": self.toggle_voice, "quiet": self.toggle_quiet}[name]()

    def on_question(self, text: str) -> None:
        from strategy import Plan
        reply = answer(text, self.snap, self.plan or Plan())
        if self.eng is not None:
            self.eng.radio.appendleft({"t": self.snap.elapsed, "text": f"Toi : {text}", "urgent": False, "me": True})
            self.eng.radio.appendleft({"t": self.snap.elapsed, "text": reply, "urgent": False})
        print(f"  [ingé] {reply}")
        self.voice.say(reply, urgent=True)


def make_source(demo: bool):
    if demo:
        return DemoSource()
    if sys.platform != "win32":
        print("La lecture en direct de LMU ne fonctionne que sous Windows. Lance avec --demo pour tester.")
        sys.exit(1)
    from telemetry import LMUSource
    return LMUSource()


def loop(state: State, demo: bool) -> None:
    source = make_source(demo)
    eng = Engineer()
    eng.on_say = state.voice.say
    state.eng = eng
    questions.VOICE, questions.ENG = state.voice, eng
    lt = LapTimes()
    questions.LAPTIMES = lt
    beh = Behavior(state.map)
    beh.say = lambda key, text, **kw: eng.say(key, text, elapsed=state.snap.elapsed)
    questions.BEHAVIOR = beh
    state.beh = beh
    probe = Probe() if not demo else None
    say = lambda key, text, urgent=False: eng.say(key, text, urgent=urgent, elapsed=state.snap.elapsed)
    spotter = Spotter(state.voice)
    spotter.enabled = state.radio.cfg.get("spotter", True)
    state.spotter = spotter
    traffic, trend, limits, damage = Traffic(beh), GapTrend(), TrackLimits(beh), Damage()
    warmup, carinfo, places, morale = WarmUp(), CarInfo(), Places(), Morale()
    set_encourage(state.radio.cfg.get("encourage", "normal"))
    weather = Weather(demo)
    recorder = debrief.Recorder()
    session_end = {"key": None, "prompt": 0}
    crew = PitCrew(state.radio.cfg, demo)
    crew.say = say
    state.crew = crew
    pen, under, pace = Penalties(), Undercut(), Pace()
    state.pace = pace
    questions.PIT_ACTION = lambda: crew.prepare(state.snap, state.plan, asked=True)

    def set_repair(want: bool, full: bool = True) -> bool:
        """Le pilote dit « répare » / « pas de réparation » : prioritaire jusqu'au prochain arrêt."""
        damage.choice, damage.choice_all = want, full
        for o in (state.snap, state.plan):
            if o is not None:
                o.repair, o.no_repair, o.repair_all = want, not want, want and full
        # menu des stands déjà réglé pour cet arrêt : on le refait tout de suite
        if crew.done_for == state.snap.pit_stops and state.snap.on_track and not state.snap.in_pits:
            crew.prepare(state.snap, state.plan, asked=True)
            return True
        return False

    questions.REPAIR_SET = set_repair

    def setup_sync():
        """Toutes les 15 s : quel setup est chargé dans LMU ? (sélection automatique dans l'atelier)"""
        while not demo:
            try:
                if state.snap.connected and state.snap.track:
                    f = state.workshop.sync_active(state.snap.track)
                    if f:   # un autre setup a remplacé celui qu'on analysait
                        beh.reset_analysis()
                    check_setup_class()
            except Exception:
                traceback.print_exc()
            time.sleep(15)

    warned_setup = set()

    def check_setup_class():
        """Le setup chargé a-t-il été fait pour cette version de la voiture (LMP2 / LMP2 ELMS…) ?"""
        ws, sn = state.workshop, state.snap
        ws.car_class, ws.car = sn.car_class, sn.car
        m = ws.class_mismatch(sn.track)
        ws.mismatch = m
        if not m:
            return
        k = (sn.track, m["file"], m["car"])
        if k in warned_setup:
            return
        warned_setup.add(k)
        say(None, i18n.L(
            f"Attention, ton setup {m['file'][:-4]} a été fait pour la {car_label(m['setup'])}, "
            f"mais tu es en {car_label(m['car'], sn.car)}. Charge plutôt un setup fait pour cette voiture.",
            f"Careful, your setup {m['file'][:-4]} was made for the {car_label(m['setup'])}, "
            f"but you're in the {car_label(m['car'], sn.car)}. Better load a setup made for this car.",
            f"Dikkat, {m['file'][:-4]} adlı setup'ın {car_label(m['setup'])} için hazırlanmış, " f"ama sen {car_label(m['car'], sn.car)} kullanıyorsun. Bu araç için hazırlanmış bir setup yüklesen iyi olur."), urgent=False)

    threading.Thread(target=setup_sync, daemon=True).start()
    inc = Incidents(beh)
    inc.say = lambda key, text, urgent=False: eng.say(key, text, urgent=urgent, elapsed=state.snap.elapsed)
    state.voice.volume = float(state.radio.cfg.get("volume", 1.3))
    if demo:
        demo_dir = make_demo_setups()
        state.workshop = Workshop({**state.radio.cfg, "setups_dir": str(demo_dir), "setup_folder_for": {}})
    else:
        state.workshop = Workshop(state.radio.cfg)
    state.workshop.scan(force=True)
    print(f"  Setups LMU : {state.workshop.dir} ({'trouvé' if state.workshop.ok() else 'introuvable'})")
    last_lap_seen = None

    def fast_loop():
        """Lecture rapide (50 fois/s) de la dynamique pour l'analyse du comportement."""
        k = 0
        while True:
            try:
                beh.feed(source.fast_sample())
                k += 1
                if k % 2 == 0:
                    spotter.feed(source.spot_sample())
            except Exception:
                traceback.print_exc()
                time.sleep(1)
            time.sleep(0.02)

    threading.Thread(target=fast_loop, daemon=True).start()
    while True:
        try:
            snap = source.read()
            damage.update(snap, say)
            plan = eng.update(snap)
            lt.update(snap, eng.say)
            state.map.update(snap)
            spotter.session = snap.session
            inc.update(snap)
            traffic.update(snap, plan, say)
            trend.update(snap, say)
            limits.update(snap, say)
            warmup.update(snap, say)
            places.update(snap, say)
            morale.update(snap, say)
            carinfo.update(snap, say)
            if state.workshop:
                state.workshop.car_class, state.workshop.car = snap.car_class, snap.car
            weather.update(snap, say)
            crew.update(snap, plan)
            pen.update(snap, say)
            under.update(snap, plan, say)
            pace.update(snap, eng, say)
            state.fuel.update(snap, plan, eng)
            # fin de session (drapeau à damier) : rappel des réglages garage + ouverture de l'atelier
            key = (snap.track, snap.session_code)
            if snap.on_track and snap.done and session_end["key"] != key and snap.laps_done >= 3:
                session_end["key"] = key
                msg = beh.end_of_session()
                if msg:
                    say(None, msg)
                    session_end["prompt"] += 1
            recorder.update(snap, lt, beh, eng, state.workshop.state(snap.track).get("label") if snap.track else None)
            if probe:
                probe.update(snap)
            state.snap, state.plan = snap, plan
            if snap.track:
                state.last_track = snap.track
            if snap.session == "qualif":
                state.workshop.set_mode("Qualif")
            elif snap.session == "course":
                state.workshop.set_mode("Course")
            L = lt.last
            if L and L.get("lap") != last_lap_seen:
                last_lap_seen = L.get("lap")
                if L.get("valid"):
                    state.workshop.record_lap(snap.track, L["lap"], beh.issues)
            if demo and isinstance(source, DemoSource) and plan.laps_until_box is not None \
                    and plan.laps_until_box <= 1 and snap.lap_frac > 0.6 and not snap.in_pits:
                source.request_pit(plan.fuel_to_add or 0, plan.energy_target, bool(plan.change_tyres or plan.tyre_compound))
            data = {
                "demo": demo,
                "snap": asdict(snap),
                "plan": asdict(plan),
                "radio": list(eng.radio),
                "accent": state.radio.cfg.get("accent", "bleu"),
                "encourage": state.radio.cfg.get("encourage", "normal"),
                "autolaunch_lmu": bool(state.radio.cfg.get("autolaunch_lmu")),
                "voice": {"on": state.voice.enabled, "engine": state.voice.engine, "volume": state.voice.volume,
                          "quiet": state.voice.quiet, "choice": voices.current_id(), "list": voices.catalog(),
                          "name": voices.engineer_name(),
                          "downloading": state.voice.downloading},
                "penalty": pen.ui(snap),
                "phone": PHONE_URL,
                "lang": i18n.LANG,
                "update": UPD.ui(),
                "ptt": state.radio.info(),
                "times": lt.ui(snap),
                "map": state.map.info(),
                "behavior": beh.ui(),
                "incidents": inc.active,
                "forecast": weather.ui(snap),
                "spotter": {"on": spotter.enabled, "state": spotter.state},
                "pit": {"mode": crew.mode, "status": crew.status},
                "setup_prompt": session_end["prompt"],
                "ts": time.time(),
            }
        except Exception as e:  # on ne plante jamais pendant une course
            traceback.print_exc()
            data = {"demo": demo, "snap": asdict(Snapshot()), "plan": {}, "radio": [], "error": str(e)}
        with state.lock:
            state.payload = json.dumps(data, default=lambda o: None).encode()
        time.sleep(0.2)


DEMO_SVM = """VehicleClassSetting="Demo Hypercar"
//Setup de démonstration Apex Radio (valeurs fictives)

[GENERAL]
Notes=""
Symmetric=1
FuelSetting=80//90.0L

[FRONTWING]
FWSetting={fw}//{fwd}

[REARWING]
RWSetting={rw}//P{rwd}

[SUSPENSION]
FrontAntiSwaySetting={farb}//{farbd}
RearAntiSwaySetting={rarb}//{rarbd}

[CONTROLS]
RearBrakeSetting={bias}//{biasd}
TractionControlMapSetting={tc}//{tc}

[DRIVELINE]
DiffPowerSetting={dp}//{dpd}
DiffCoastSetting={dc}//{dcd}

""" + "".join(f"""[{w}]
CamberSetting={{cam}}//{{camd}}
PressureSetting={{pr}}//{{prd}}
RideHeightSetting={{rh}}//{{rhd}}

""" for w in ("FRONTLEFT", "FRONTRIGHT", "REARLEFT", "REARRIGHT"))


def make_demo_setups() -> Path:
    """Dossier de setups fictifs pour tester l'atelier en mode démo."""
    d = HERE / "demo_setups" / "Circuit de demo"
    d.mkdir(parents=True, exist_ok=True)
    arb = ["D18 S-S", "D18 S-H", "D18 H-H", "D22 S-S", "D22 S-H", "D22 H-H"]
    diff = ["FF6-60 deg", "FF6-55 deg", "FF6-45 deg", "FF6-30 deg", "FF6-25 deg"]
    for name, k in (("Demo base.svm", 2), ("Demo ancien.svm", 3)):
        f = d / name
        if f.exists():
            continue
        f.write_text(DEMO_SVM.format(
            fw=k, fwd=f"{k} deg", rw=k, rwd=k + 1, farb=k, farbd=arb[k], rarb=k - 1, rarbd=arb[k - 1],
            bias=14 + k, biasd=f"{54.5 - 0.45 * k:.1f}:{45.5 + 0.45 * k:.1f}", tc=3 + k, dp=k, dpd=diff[k], dc=k, dcd=diff[k],
            cam=10 + k, camd=f"{-1.8 + 0.1 * k:.1f} deg", pr=k, prd=f"{130 + 2 * k} kPa", rh=8 + k, rhd=f"{4.0 + 0.1 * k:.1f} cm"),
            encoding="utf-8")
    return d.parent


def make_handler(state: State):
    html = (HERE / "ui" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/mics") or self.path.startswith("/api/map"):
                data = state.radio.mics() if self.path.startswith("/api/mics") else {**state.map.info(), "points": state.map.points()}
                body, ctype = json.dumps(data).encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/debrief") or self.path.rstrip("/") == "/records":
                if self.path.rstrip("/") == "/records":
                    body = debrief.records_page().encode()
                elif self.path.rstrip("/") == "/debriefs":
                    body = debrief.index_page().encode()
                else:
                    name = self.path.split("/")[-1]
                    f = debrief.DIR / f"{name}.json"
                    if not name.replace("_", "").replace("-", "").isalnum() or not f.exists():
                        self.send_error(404)
                        return
                    # rendu à la demande : la page suit la langue choisie
                    body = debrief.render(json.loads(f.read_text(encoding="utf-8"))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/setup"):
                ws, beh = state.workshop, state.beh
                if ws is None:
                    data = {"ok": False}
                else:
                    tr = state.track()
                    fuel = state.fuel.suggest(tr)
                    data = {**ws.propose(tr, beh.issues, beh.tyre_findings, beh.live_controls(tr), fuel),
                            "folders": ws.folders(), "analysis_laps": beh.laps_seen, "fuel": fuel}
                body = json.dumps(data, default=lambda o: None).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/state"):
                with state.lock:
                    body = state.payload
                ctype = "application/json"
            elif self.path == "/favicon.ico":
                body, ctype = (HERE / "apex.ico").read_bytes(), "image/x-icon"
            elif self.path.rstrip("/") in ("/tel", "/phone"):
                body, ctype = (HERE / "ui" / "tel.html").read_bytes(), "text/html; charset=utf-8"
            elif self.path in ("/", "/index.html"):
                # relu à chaque fois : une mise à jour du tableau de bord s'affiche sans relancer l'appli
                body, ctype = (HERE / "ui" / "index.html").read_bytes(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            v = state.voice
            if self.path == "/api/voice/toggle":
                state.toggle_voice()
            elif self.path == "/api/lang":
                n = int(self.headers.get("Content-Length") or 0)
                lang = json.loads(self.rfile.read(n) or b"{}").get("lang", "fr")
                i18n.set_lang(lang)
                state.radio.cfg["lang"] = i18n.LANG
                save_config(state.radio.cfg)
                v.flush()
                v.say(L("Radio en français.", "Radio in English.", "Telsiz artık Türkçe."))
            elif self.path == "/api/voice/quiet":
                state.toggle_quiet()
            elif self.path == "/api/setup/race":
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                state.fuel.set_race(state.track(), req.get("minutes"), req.get("laps"))
            elif self.path == "/api/pit/mode":
                n = int(self.headers.get("Content-Length") or 0)
                mode = json.loads(self.rfile.read(n) or b"{}").get("mode")
                crew = getattr(state, "crew", None)
                if crew and mode in ("auto", "demande", "off"):
                    crew.mode = mode
                    state.radio.cfg["pit_auto"] = mode
                    save_config(state.radio.cfg)
            elif self.path == "/api/pit/now":
                crew = getattr(state, "crew", None)
                if crew:
                    crew.prepare(state.snap, state.plan, asked=True)
            elif self.path == "/api/spotter/toggle":
                state.toggle_spotter()
            elif self.path == "/api/show":
                desktop.open_window(f"http://localhost:{PORT}")
            elif self.path == "/api/quit":
                self.send_response(204)
                self.end_headers()
                print("  Arrêt demandé depuis le tableau de bord.")
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            elif self.path == "/api/voice/volume":
                n = int(self.headers.get("Content-Length") or 0)
                vol = float(json.loads(self.rfile.read(n) or b"{}").get("v", 1.3))
                v.volume = max(0.6, min(1.6, vol))
                state.radio.cfg["volume"] = v.volume
                save_config(state.radio.cfg)
                v.say(L("Radio check, niveau du son.", "Radio check, volume level.", "Telsiz kontrol, ses düzeyi."))
            elif self.path == "/api/ptt/learn":
                n = int(self.headers.get("Content-Length") or 0)
                target = (json.loads(self.rfile.read(n) or b"{}") if n else {}).get("target", "ptt")
                state.radio.start_learning(target)
            elif self.path == "/api/lmu/launch":
                launch_lmu()
            elif self.path == "/api/lmu/autolaunch":
                n = int(self.headers.get("Content-Length") or 0)
                state.radio.cfg["autolaunch_lmu"] = bool(json.loads(self.rfile.read(n) or b"{}").get("on"))
                save_config(state.radio.cfg)
            elif self.path == "/api/encourage":
                n = int(self.headers.get("Content-Length") or 0)
                lv = json.loads(self.rfile.read(n) or b"{}").get("level", "normal")
                if lv in ("discret", "normal", "motivant"):
                    state.radio.cfg["encourage"] = lv
                    save_config(state.radio.cfg)
                    set_encourage(lv)
            elif self.path in ("/api/setup/dir", "/api/setup/pickdir"):
                n = int(self.headers.get("Content-Length") or 0)
                want = (json.loads(self.rfile.read(n) or b"{}").get("dir") or "").strip().strip('"')
                if self.path == "/api/setup/pickdir":
                    want = pick_folder() or ""
                res = set_setups_dir(state, want) if want else {"ok": False, "cancel": True}
                body = json.dumps(res).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            elif self.path == "/api/accent":
                n = int(self.headers.get("Content-Length") or 0)
                a = json.loads(self.rfile.read(n) or b"{}").get("accent", "bleu")
                if a in ("bleu", "orange", "vert", "rouge", "violet", "jaune", "blanc"):
                    state.radio.cfg["accent"] = a
                    save_config(state.radio.cfg)
            elif self.path == "/api/shortcut/clear":
                n = int(self.headers.get("Content-Length") or 0)
                state.radio.clear_shortcut(json.loads(self.rfile.read(n) or b"{}").get("target", ""))
            elif self.path in ("/api/setup/select", "/api/setup/create", "/api/setup/restore", "/api/setup/mode"):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                ws, tr = state.workshop, state.track()
                if req.get("mode"):
                    ws.set_mode(req["mode"])
                if self.path.endswith("mode"):
                    out = {"error": None}
                elif self.path.endswith("restore"):
                    out = ws.restore(tr, int(req.get("version") or 0))
                    if not out.get("error"):
                        state.beh.reset_analysis()
                        state.eng.say(None, L(f"Setup remis en version {out['version'][1:]}. Recharge-le au garage.",
                                              f"Setup restored to version {out['version'][1:]}. Reload it in the garage.",
                                              f"Setup {out['version'][1:]}. sürüme döndürüldü. Garajdan yeniden yükle."), elapsed=state.snap.elapsed)
                elif self.path.endswith("select"):
                    prev = ws.state(tr)
                    ws.select(tr, req.get("folder"), req.get("file"))
                    if not state.demo:
                        save_config(state.radio.cfg)
                    # tu indiques le setup avec lequel tu viens de rouler : l'analyse le concerne, on la garde.
                    # On ne repart de zéro que si tu remplaces un setup déjà choisi par un autre.
                    if prev.get("current") and (prev.get("folder"), prev.get("current")) != (req.get("folder"), req.get("file")):
                        state.beh.reset_analysis()
                    out = {"error": None}
                else:
                    prop = ws.propose(tr, state.beh.issues, state.beh.tyre_findings, state.beh.live_controls(tr),
                                      state.fuel.suggest(tr))
                    keep = set(req.get("keys") or [])
                    chosen = [c for c in prop["changes"] if f"{c['section']}|{c['key']}" in keep]
                    out = ws.create(tr, chosen)
                    if not out.get("error"):
                        state.beh.reset_analysis()
                        state.eng.say(None, L(f"Setup {ws.mode.lower()} mis à jour, version {out['version'][1:]}. Recharge-le au garage, je refais l'analyse.",
                                              f"{'Qualifying' if ws.mode == 'Qualif' else 'Race'} setup updated, version {out['version'][1:]}. "
                                              "Reload it in the garage, I'll redo the analysis.",
                                              f"{'Sıralama' if ws.mode == 'Qualif' else 'Yarış'} setup'ı güncellendi, {out['version'][1:]}. sürüm. " "Garajdan yeniden yükle, analizi baştan yapacağım."),
                                      elapsed=state.snap.elapsed)
                body = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            elif self.path == "/api/mic":
                n = int(self.headers.get("Content-Length") or 0)
                idx = json.loads(self.rfile.read(n) or b"{}").get("index")
                state.radio.set_mic(idx)
            elif self.path == "/api/ask":
                n = int(self.headers.get("Content-Length") or 0)
                q = json.loads(self.rfile.read(n) or b"{}").get("q", "")
                threading.Thread(target=state.on_question, args=(q,), daemon=True).start()
            elif self.path == "/api/voice/choose":
                n = int(self.headers.get("Content-Length") or 0)
                vid = json.loads(self.rfile.read(n) or b"{}").get("id", "")
                if v.choose(vid):
                    state.radio.cfg["voices"] = dict(voices.CHOICE)
                    save_config(state.radio.cfg)
                    name = voices.engineer_name()
                    if all(f.exists() for f in v._files(vid)):
                        v.say(voices.L_ready(name), urgent=True)
                    else:   # l'ancienne voix prévient ; la nouvelle se présentera une fois téléchargée
                        v.say(L(f"Je télécharge la voix de {name}, {'elle' if voices.engineer_female() else 'il'} prend la radio dans une minute.",
                                f"Downloading {name}'s voice, it'll take over the radio in a minute.",
                                f"{name} sesi indiriliyor, bir dakika içinde telsizi devralacak."), urgent=True)
            elif self.path == "/api/voice/test":
                v.say(L("Radio check. Tu m'entends bien ? On est prêts pour la course.", "Radio check. Do you copy? We're ready to race.", "Telsiz kontrol. Beni duyuyor musun? Yarışa hazırız."))
            else:
                self.send_error(404)
                return
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    return Handler


PHONE_URL = ""
UPD = updates.Updates()


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class ApexServer(ThreadingHTTPServer):
    """Serveur du tableau de bord, en exclusivité sur son port.

    Sous Windows, l'option « réutiliser l'adresse » permet à un 2e Apex Radio d'écouter sur le même port :
    les pages viennent alors au hasard de l'un ou de l'autre. On la remplace par l'accès exclusif."""
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def already_running() -> bool:
    """Un Apex Radio répond déjà sur le port ?"""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def replace_other_mode(demo: bool) -> bool:
    """Le Apex Radio déjà lancé est dans l'autre mode (démo / course) : on l'arrête pour démarrer celui demandé."""
    import urllib.request
    base = f"http://127.0.0.1:{PORT}"
    try:
        with urllib.request.urlopen(base + "/api/state", timeout=2) as r:
            other_demo = bool(json.load(r).get("demo"))
    except Exception:
        return False
    if other_demo == demo:
        return False
    print("  Un Apex Radio " + ("en course" if not other_demo else "en démo") + " tourne déjà : je l'arrête.")
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/api/quit", data=b"{}", method="POST"), timeout=3).close()
    except Exception:
        pass
    for _ in range(40):
        time.sleep(0.25)
        if not already_running():
            return True
    return False


def ensure_packages() -> None:
    """Installe les composants manquants et note le résultat dans journal.txt."""
    import importlib.util
    import subprocess
    needed = {"piper": "piper-tts", "faster_whisper": "faster-whisper", "sounddevice": "sounddevice", "pygame": "pygame-ce"}
    if sys.platform == "win32":
        needed.update({"pystray": "pystray", "PIL": "Pillow", "webview": "pywebview"})
    missing = [pkg for mod, pkg in needed.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"  Installation des composants manquants : {', '.join(missing)} (quelques minutes)…")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing],
                       capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(r.stdout[-3000:])
    if r.returncode:
        print("  ÉCHEC de l'installation :")
        print(r.stderr[-4000:])
    else:
        print("  Composants installés.")


class _Tee:
    """Écrit à l'écran et dans journal.txt (pour le dépannage)."""
    def __init__(self, stream, f):
        self.stream, self.f = stream, f
    def write(self, data):
        try:
            self.stream.write(data)
        except Exception:
            pass
        self.f.write(data); self.f.flush()
    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass


LMU_STEAM_ID = 2399420


def _steam_exe() -> str | None:
    """Chemin de steam.exe (registre Windows), pour lancer LMU sans passer par le lien steam://."""
    try:
        import winreg
        for root, key, name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamExe"),
                                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(root, key) as k:
                    v = str(winreg.QueryValueEx(k, name)[0])
                p = Path(v) if v.lower().endswith(".exe") else Path(v) / "steam.exe"
                if p.exists():
                    return str(p)
            except OSError:
                continue
    except Exception:
        pass
    return None


def pick_folder() -> str | None:
    """Fenêtre Windows « Choisir un dossier » (par PowerShell, rien à installer)."""
    if sys.platform != "win32":
        return None
    import subprocess
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$o=New-Object System.Windows.Forms.Form;$o.TopMost=$true;"
          "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
          "$f.Description='Dossier Le Mans Ultimate (ou UserData\\player\\Settings)';"
          "if($f.ShowDialog($o) -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;$f.SelectedPath}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.decode("utf-8", "ignore").strip() or None
    except Exception:
        return None


def set_setups_dir(state, raw: str) -> dict:
    """Dossier des setups choisi à la main : on accepte aussi le dossier du jeu ou UserData."""
    p = Path(raw)
    for c in (p / "UserData" / "player" / "Settings", p / "player" / "Settings", p / "Settings", p):
        if c.is_dir() and (c.name.lower() == "settings" or any(c.glob("*/*.svm"))):
            state.radio.cfg["setups_dir"] = str(c)
            save_config(state.radio.cfg)
            state.workshop.dir = c
            try:
                state.workshop.scan(force=True)
            except Exception:
                pass
            return {"ok": True, "dir": str(c)}
    return {"ok": False, "error": i18n.L("Ce dossier ne contient pas les setups de LMU.", "This folder doesn't contain LMU setups.", "Bu klasörde LMU setup'ı yok.")}


def launch_lmu() -> bool:
    """Lance Le Mans Ultimate par Steam : steam.exe -applaunch (pas de fenêtre de confirmation),
    sinon le lien steam://rungameid/…"""
    url = f"steam://rungameid/{LMU_STEAM_ID}"
    try:
        if sys.platform == "win32":
            exe = _steam_exe()
            if exe:
                import subprocess
                subprocess.Popen([exe, "-applaunch", str(LMU_STEAM_ID)], close_fds=True,
                                 creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            else:
                os.startfile(url)
        else:
            import webbrowser
            webbrowser.open(url)
        print("  Lancement de Le Mans Ultimate par Steam…")
        return True
    except Exception as e:
        print(f"  Impossible de lancer LMU : {e!r}")
        return False


def autolaunch_lmu(state) -> None:
    """Option : si LMU n'est pas déjà lancé quelques secondes après l'ouverture d'Apex, on le démarre."""
    time.sleep(6)
    if state.radio.cfg.get("autolaunch_lmu") and not getattr(state.snap, "connected", False):
        launch_lmu()


def main() -> None:
    log = open(LOCAL / "journal.txt", "w", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)
    print(f"Python {sys.version.split()[0]} · {sys.platform}")
    ensure_packages()
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="course simulée, sans le jeu")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-tray", action="store_true", help="pas d'icône près de l'horloge")
    args = ap.parse_args()

    global PHONE_URL
    PHONE_URL = f"http://{local_ip()}:{PORT}/tel"
    if already_running() and not replace_other_mode(args.demo):
        # avant de charger la voix : on rouvre juste la fenêtre existante
        print("\n  Apex Radio tourne déjà : j'ouvre le tableau de bord existant.")
        desktop.show_existing(f"http://localhost:{PORT}")
        sys.exit(0)
    state = State()
    state.demo = args.demo
    try:
        server = ApexServer(("0.0.0.0", PORT), make_handler(state))
    except OSError:
        print("\n  Apex Radio tourne déjà : j'ouvre le tableau de bord existant.")
        print("  (pour passer en démo ou en direct, clique d'abord sur QUITTER dans le tableau de bord)\n")
        desktop.show_existing(f"http://localhost:{PORT}")
        sys.exit(0)
    threading.Thread(target=loop, args=(state, args.demo), daemon=True).start()
    if not args.demo:
        threading.Thread(target=autolaunch_lmu, args=(state,), daemon=True).start()
    UPD.start()
    url = f"http://localhost:{PORT}"
    print("=" * 56)
    print(f"  APEX//RADIO {UPD.version} — " + ("MODE DÉMO" if args.demo else "Le Mans Ultimate"))
    print(f"  Tableau de bord : {url}")
    print(f"  Sur téléphone : http://{local_ip()}:{PORT}/tel")
    print(f"  Tes données : {DATA}")
    if MIGRATED:
        print(f"  Données reprises de l'ancien dossier : {', '.join(MIGRATED)}")
    print("=" * 56)
    desktop.app_id()
    tray = desktop.Tray(state, url, PHONE_URL, state.radio.cfg, save_config)
    tray_ok = not args.no_tray and tray.available()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if tray_ok:
        state.tray = tray
        tray.run(detached=True)   # icône près de l'horloge, à côté de la fenêtre
    win = desktop.AppWindow(url, hide_on_close=tray_ok, on_hidden=tray.hint if tray_ok else None)
    if not args.no_browser and win.available():
        try:
            win.run()             # fenêtre Apex Radio, jusqu'à « Quitter »
        except Exception as e:
            print(f"  Fenêtre intégrée impossible ({e.__class__.__name__}: {e}), fenêtre Edge à la place.")
            desktop.WINDOW = None
            desktop.open_app_window(url)
    elif not args.no_browser:
        threading.Timer(1.0, lambda: desktop.open_app_window(url)).start()
    try:
        threading.Event().wait()  # la radio tourne jusqu'à « Quitter » (bouton ⏻ ou icône)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
