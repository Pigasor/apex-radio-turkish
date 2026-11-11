"""
Bouton radio + micro + reconnaissance vocale (étape 3).

- Bouton : n'importe quel bouton du volant / de la box (via SDL, pygame-ce),
  ou une touche du clavier. Fonctionne même quand le jeu est au premier plan.
- Micro : micro par défaut de Windows, enregistré tant que le bouton est appuyé.
- Reconnaissance : Whisper (faster-whisper, modèle "small", français, sur le processeur).
  Le modèle (~480 Mo) est téléchargé une seule fois au premier lancement.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
from paths import DATA, LOCAL  # noqa: E402
CONFIG = DATA / "config.json"
WHISPER_MODEL = "small"
WHISPER_FILES = ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin")   # model.bin en dernier : sa présence = modèle complet
RATE = 16000
import i18n
from i18n import L

PROMPT_EN = ("Race car radio. Fuel, consumption, energy, tyres, wear, pit, box, stop, "
             "gap ahead, behind, position, weather, rain, laps left, lap time.")
PROMPT_TR = ("Yarış telsizi. Yakıt, tüketim, enerji, lastik, aşınma, pit, box, duruş, "
             "öndeki fark, arkadaki, pozisyon, hava durumu, yağmur, kalan tur, tur zamanı, sektör, hasar, tamir.")
PROMPT = ("Radio course automobile. Essence, conso, énergie, pneus, usure, stand, box, arrêt, "
          "écart devant, derrière, position, météo, pluie, tours restants, chrono.")


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- clavier
def _keys_down() -> set[int]:
    if sys.platform != "win32":
        return set()
    import ctypes
    gk = ctypes.windll.user32.GetAsyncKeyState
    # on ignore la souris (1-6), Windows (91, 92), Maj/Ctrl/Alt (16-18, 160-165), Échap (27)
    ignore = {0x10, 0x11, 0x12, 0x1B, 0x5B, 0x5C, 0x5D} | set(range(0xA0, 0xA6))
    return {vk for vk in range(8, 255) if vk not in ignore and gk(vk) & 0x8000}


KEY_NAMES = {0x20: "Espace", 0x0D: "Entrée", 0x10: "Maj", 0x11: "Ctrl", 0x12: "Alt", 0x14: "Verr. Maj"}


def key_name(vk: int) -> str:
    if vk in KEY_NAMES:
        return KEY_NAMES[vk]
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x30 <= vk <= 0x5A:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return f"Pavé {vk - 0x60}"
    return f"Touche {vk}"


SHORTCUTS = ("spotter", "radio", "quiet")   # raccourcis : couper/allumer le spotter, la radio, le mode silence


class Radio:
    """Gère le bouton, l'écoute et la transcription. Appelle on_question(texte),
    et on_shortcut(nom) quand un raccourci (spotter, radio, silence) est pressé."""

    def __init__(self, on_question, on_shortcut=None) -> None:
        self.on_question = on_question
        self.on_shortcut = on_shortcut
        self.learn_target = "ptt"         # ce qu'on est en train d'apprendre : le bouton radio ou un raccourci
        self._short_down: dict = {}
        self.cfg = load_config()
        self.state = "démarrage"       # démarrage / prêt / écoute / réflexion / erreur / apprentissage
        self.detail = ""
        self.last_heard = ""
        self.learning = False
        self._pg = None
        self._joys = {}
        self._whisper = None
        self._rec = None
        self._frames: list = []
        self._learn_reset = False
        self.level = 0.0
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._load_whisper, daemon=True).start()

    # ------------------------------------------------------------- infos UI
    @staticmethod
    def label(b) -> str:
        if not b:
            return L("aucun", "none", "yok")
        if b["type"] == "key":
            return L(f"clavier · {key_name(b['vk'])}", f"keyboard · {key_name(b['vk'])}", f"klavye · {key_name(b['vk'])}")
        return L(f"{b['name']} · bouton {b['button'] + 1}", f"{b['name']} · button {b['button'] + 1}", f"{b['name']} · tuş {b['button'] + 1}")

    def info(self) -> dict:
        b = self.cfg.get("ptt")
        if b and b.get("type") == "key" and b.get("vk") in (0x5B, 0x5C):
            self.cfg.pop("ptt"); save_config(self.cfg); b = None
        label = self.label(b)
        sc = self.cfg.get("shortcuts") or {}
        return {"state": self.state, "detail": self.detail, "button": label, "level": self.level,
                "shortcuts": {k: (self.label(sc.get(k)) if sc.get(k) else None) for k in SHORTCUTS},
                "learn_target": self.learn_target if self.learning else None,
                "mic": self.cfg.get("mic"),
                "heard": self.last_heard, "learning": self.learning,
                "whisper": self._whisper is not None}

    def mics(self) -> list[dict]:
        """Micros disponibles (API MME de Windows : la plus compatible)."""
        try:
            import sounddevice as sd
            default_in = sd.default.device[0]
            out = []
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and d["hostapi"] == 0:
                    out.append({"index": i, "name": d["name"], "default": i == default_in})
            return out
        except Exception as e:
            return [{"index": None, "name": f"Erreur : {e!r}", "default": True}]

    def set_mic(self, index) -> None:
        self.cfg["mic"] = index
        save_config(self.cfg)
        print(f"  Micro choisi : {index}")

    def start_learning(self, target: str = "ptt") -> None:
        self.learn_target = target if target in SHORTCUTS else "ptt"
        self._learn_reset = True
        self.learning = True
        self.state = "apprentissage"

    def clear_shortcut(self, target: str) -> None:
        sc = self.cfg.setdefault("shortcuts", {})
        if target in sc:
            sc.pop(target)
            save_config(self.cfg)

    # ------------------------------------------------------------- whisper
    def _load_whisper(self) -> None:
        try:
            from faster_whisper import WhisperModel
            print("  " + L("Reconnaissance vocale : chargement…", "Speech recognition: loading…", "Ses tanıma: yükleniyor…"))
            model = self._whisper_files()
            self._whisper = WhisperModel(str(model), device="cpu", compute_type="int8")
            self.detail = ""
            print("  " + L("Reconnaissance vocale : prête", "Speech recognition: ready", "Ses tanıma: hazır"))
        except Exception as e:
            import traceback; traceback.print_exc()
            self.detail = L(f"Reconnaissance vocale indisponible : {e!r}", f"Speech recognition unavailable: {e!r}", f"Ses tanıma kullanılamıyor: {e!r}")
            print("  " + self.detail)

    def _whisper_files(self) -> Path:
        """Dossier du modèle Whisper. Téléchargé une seule fois, fichier par fichier, avec la progression
        affichée (console + tableau de bord) : sinon le téléchargement de ~480 Mo a l'air bloqué."""
        root = LOCAL / "models" / "whisper"
        # déjà là : notre dossier, ou le cache Hugging Face des anciennes versions
        for d in [root / WHISPER_MODEL, *root.glob(f"models--Systran--faster-whisper-{WHISPER_MODEL}/snapshots/*")]:
            if all((d / f).exists() for f in WHISPER_FILES):
                return d
        import urllib.request
        dest = root / WHISPER_MODEL
        dest.mkdir(parents=True, exist_ok=True)
        base = f"https://huggingface.co/Systran/faster-whisper-{WHISPER_MODEL}/resolve/main/"
        for f in WHISPER_FILES:
            out = dest / f
            if out.exists():
                continue
            part = out.with_name(f + ".part")
            # serveur parfois très lent ou qui se fige : on reprend là où on s'est arrêté (aussi au prochain lancement)
            for attempt in range(30):
                done = part.stat().st_size if part.exists() else 0
                req = urllib.request.Request(base + f, headers={"Range": f"bytes={done}-"} if done else {})
                try:
                    with urllib.request.urlopen(req, timeout=30) as r, open(part, "ab" if r.status == 206 else "wb") as fh:
                        if r.status != 206:
                            done = 0
                        total, shown = done + int(r.headers.get("Content-Length") or 0), -1
                        while chunk := r.read(1 << 16):
                            fh.write(chunk)
                            done += len(chunk)
                            pct = done * 100 // total if total > 1_000_000 else -1
                            if pct >= 0 and pct != shown:
                                shown = pct
                                self.detail = L(f"Téléchargement de la reconnaissance vocale : {pct} % (~480 Mo, une seule fois)",
                                                f"Downloading speech recognition: {pct} % (~480 MB, only once)",
                                                f"Ses tanıma indiriliyor: %{pct} (~480 MB, yalnızca bir kez)")
                                print("\r  " + self.detail, end="", flush=True)
                    if total and done < total:
                        raise ConnectionError(f"{done}/{total}")
                    break
                except Exception as e:
                    if attempt == 29:
                        raise
                    print(f"\n  {f} : {e!r}, " + L("nouvel essai…", "retrying…", "yeniden deneniyor…"))
                    time.sleep(min(30, 2 + attempt * 2))
            part.replace(out)
        print()
        return dest

    def transcribe(self, audio) -> str:
        segs, _ = self._whisper.transcribe(audio, language=i18n.LANG, beam_size=1, initial_prompt={"en": PROMPT_EN, "tr": PROMPT_TR}.get(i18n.LANG, PROMPT),
                                           condition_on_previous_text=False, vad_filter=False)
        return " ".join(s.text.strip() for s in segs).strip()

    # ------------------------------------------------------------- micro
    def _start_rec(self) -> None:
        import sounddevice as sd
        self._frames = []

        def cb(indata, frames, t, status):
            self._frames.append(indata.copy())
            self.level = max(self.level * 0.8, float(abs(indata).max()))

        dev = self.cfg.get("mic")
        self.level = 0.0
        if dev is None and (sd.default.device[0] is None or sd.default.device[0] < 0):
            # Windows n'a pas de micro « par défaut » (ex. casque Xbox) : on prend le premier micro trouvé
            mics = [m for m in self.mics() if m["index"] is not None]
            if not mics:
                raise RuntimeError(L("Aucun micro trouvé. Branche ton micro ou ton casque, puis choisis-le dans la liste.",
                                     "No microphone found. Plug in your mic or headset, then pick it in the list.",
                                     "Mikrofon bulunamadı. Mikrofonunu veya kulaklığını tak, sonra listeden seç."))
            dev = mics[0]["index"]
            print(f"  Pas de micro par défaut dans Windows : j'utilise « {mics[0]['name']} »")
        name = sd.query_devices(dev if dev is not None else sd.default.device[0])["name"]
        print(f"  Écoute sur le micro : {name}")
        self._rec = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", callback=cb, device=dev)
        self._rec.start()

    def _stop_rec(self):
        import numpy as np
        if self._rec is None:
            return None
        self._rec.stop(); self._rec.close(); self._rec = None
        if not self._frames:
            return None
        return np.concatenate(self._frames)[:, 0]

    def _process(self, audio) -> None:
        try:
            if audio is None or len(audio) < RATE * 0.3:
                self.state = "prêt"
                return
            peak = float(abs(audio).max())
            print(f"  Niveau micro max : {peak * 100:.1f} %  ({len(audio) / RATE:.1f} s)")
            if peak < 0.01:
                self.state = "prêt"
                self.detail = L("Le micro n'enregistre que du silence. Choisis ton micro dans la liste ci-dessous, "
                                "et vérifie Paramètres Windows > Confidentialité > Microphone "
                                "(« Autoriser les applications de bureau »).",
                                "The mic is only recording silence. Pick your mic in the list below, "
                                "and check Windows Settings > Privacy > Microphone (\"Let desktop apps access your microphone\").",
                                "Mikrofon yalnızca sessizlik kaydediyor. Aşağıdaki listeden mikrofonunu seç " "ve Windows Ayarları > Gizlilik > Mikrofon bölümünü kontrol et (\"Masaüstü uygulamalarının mikrofonunuza erişmesine izin verin\").")
                self.on_question("")
                return
            self.detail = ""
            if self._whisper is None:
                self.state = "prêt"
                self.detail = L("La reconnaissance vocale se charge encore…", "Speech recognition is still loading…", "Ses tanıma hâlâ yükleniyor…")
                return
            self.state = "réflexion"
            t = time.time()
            text = self.transcribe(audio)
            print(f"  [toi] {text}   ({time.time() - t:.1f}s)")
            self.last_heard = text
            self.state = "prêt"
            self.on_question(text)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.state = "erreur"
            self.detail = repr(e)

    # ------------------------------------------------------------- bouton
    def _init_pygame(self) -> bool:
        try:
            os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
            os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
            import pygame
            pygame.init()
            pygame.joystick.init()
            self._pg = pygame
            return True
        except Exception as e:
            print(f"  Volants non disponibles ({e!r}), clavier seulement.")
            return False

    def _refresh_joys(self) -> None:
        pg = self._pg
        if pg is None:
            return
        for ev in pg.event.get():
            if ev.type == pg.JOYDEVICEADDED:
                j = pg.joystick.Joystick(ev.device_index)
                self._joys[j.get_instance_id()] = j
                print(f"  Manette détectée : {j.get_name()} ({j.get_numbuttons()} boutons)")
            elif ev.type == pg.JOYDEVICEREMOVED:
                self._joys.pop(ev.instance_id, None)

    def _joy_buttons(self) -> list[tuple]:
        out = []
        for j in self._joys.values():
            try:
                for b in range(j.get_numbuttons()):
                    if j.get_button(b):
                        out.append((j.get_guid(), j.get_name(), b))
            except Exception:
                pass
        return out

    def _ptt_pressed(self) -> bool:
        return self._pressed(self.cfg.get("ptt"))

    def _pressed(self, b) -> bool:
        if not b:
            return False
        if b["type"] == "key":
            if sys.platform != "win32":
                return False
            import ctypes
            return bool(ctypes.windll.user32.GetAsyncKeyState(b["vk"]) & 0x8000)
        # Certains volants (ex. Fanatec) apparaissent deux fois dans Windows avec le même identifiant,
        # et un seul des deux envoie les boutons : on regarde donc tous les périphériques qui correspondent.
        for j in self._joys.values():
            try:
                if j.get_guid() == b["guid"] and b["button"] < j.get_numbuttons() and j.get_button(b["button"]):
                    return True
            except Exception:
                pass
        return False

    def _learn(self, held_keys: set[int], held_btns: set[tuple]) -> tuple[set, set]:
        keys = _keys_down()
        btns = set(self._joy_buttons())
        new_btn = btns - held_btns
        new_key = keys - held_keys
        if new_btn:
            guid, name, b = sorted(new_btn)[0]
            binding = {"type": "joy", "guid": guid, "name": name, "button": b}
        elif new_key:
            binding = {"type": "key", "vk": sorted(new_key)[0]}
        else:
            return keys, btns
        if self.learn_target in SHORTCUTS:
            if binding == self.cfg.get("ptt"):
                self.detail = L("Ce bouton sert déjà à la radio : choisis-en un autre.",
                                "That button is already your radio button: pick another one.",
                                "Bu tuş zaten telsiz tuşun: başka birini seç.")
            else:
                sc = self.cfg.setdefault("shortcuts", {})
                for k in SHORTCUTS:                    # un bouton = une seule action
                    if sc.get(k) == binding:
                        sc.pop(k)
                sc[self.learn_target] = binding
                self._short_down[self.learn_target] = True   # ne pas déclencher l'action au moment où on l'apprend
                print(f"  Raccourci {self.learn_target} enregistré : {self.label(binding)}")
        else:
            self.cfg["ptt"] = binding
            for k, v in list((self.cfg.get("shortcuts") or {}).items()):
                if v == binding:
                    self.cfg["shortcuts"].pop(k)
            print(f"  Bouton radio enregistré : {self.label(binding)}")
        save_config(self.cfg)
        self.learning = False
        self.learn_target = "ptt"
        self.state = "prêt"
        return keys, btns

    def _check_shortcuts(self) -> None:
        sc = self.cfg.get("shortcuts") or {}
        for name in SHORTCUTS:
            now = self._pressed(sc.get(name)) if sc.get(name) else False
            if now and not self._short_down.get(name) and self.on_shortcut:
                try:
                    self.on_shortcut(name)
                except Exception:
                    import traceback; traceback.print_exc()
            self._short_down[name] = now

    def _run(self) -> None:
        self._init_pygame()
        self.state = "prêt"
        pressed = False
        held_keys, held_btns = _keys_down(), set()
        while True:
            try:
                self._refresh_joys()
                if self.learning and self._learn_reset:
                    held_keys, held_btns = _keys_down(), set(self._joy_buttons())
                    self._learn_reset = False
                elif self.learning:
                    held_keys, held_btns = self._learn(held_keys, held_btns)
                    time.sleep(0.02)
                    continue
                self._check_shortcuts()
                now = self._ptt_pressed()
                if now and not pressed and self.state in ("prêt", "erreur"):
                    try:
                        self._start_rec()
                        self.state = "écoute"
                        self.detail = ""
                    except Exception as e:
                        self.state = "erreur"
                        if isinstance(e, RuntimeError):
                            self.detail = str(e)
                        else:
                            self.detail = L(f"Micro indisponible : {e!r}. Choisis ton micro dans la liste ci-dessous, et vérifie "
                                            "Paramètres Windows > Son > Entrée (micro par défaut) et Confidentialité > Microphone.",
                                            f"Microphone unavailable: {e!r}. Pick your mic in the list below, and check "
                                            "Windows Settings > Sound > Input (default mic) and Privacy > Microphone.",
                                            f"Mikrofon kullanılamıyor: {e!r}. Aşağıdaki listeden mikrofonunu seç ve " "Windows Ayarları > Ses > Giriş (varsayılan mikrofon) ile Gizlilik > Mikrofon bölümlerini kontrol et.")
                        print("  " + self.detail)
                elif not now and pressed and self.state == "écoute":
                    audio = self._stop_rec()
                    threading.Thread(target=self._process, args=(audio,), daemon=True).start()
                pressed = now
            except Exception as e:
                import traceback; traceback.print_exc()
                self.detail = repr(e)
            time.sleep(0.02)
