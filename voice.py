"""
Voix de l'ingénieur (étape 2).

- Voix principale : Piper (gratuit, local). Plusieurs voix au choix (menu « Voix » du tableau de bord),
  par défaut « Jessica » en français et « LJ » en anglais. Chaque fichier de voix (~60 à 115 Mo)
  est téléchargé une seule fois, la première fois qu'on la choisit.
- Effet radio : filtre passe-bande + légère saturation + petit "clic" de micro.
- Secours : si Piper n'est pas disponible, la voix intégrée de Windows prend le relais.

Les messages urgents (box, neutralisation) passent devant les autres.
"""

from __future__ import annotations

import io
import math
import queue
import re
import subprocess
import sys
import threading
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).parent
from paths import DATA, LOCAL  # noqa: E402
MODELS = LOCAL / "models"
import i18n

# Voix proposées : seulement des licences libres sans restriction commerciale (vérifié sur les MODEL_CARD de Piper).
# id -> (langue, nom du modèle, dossier sur Hugging Face, n° de voix, nom affiché, femme ?, taille en Mo)
VOICES = {
    "jessica":  ("fr", "fr_FR-upmc-medium", "fr/fr_FR/upmc/medium/", 0, "Jessica", True, 77),   # CC BY-SA 4.0 (MaryTTS upmc)
    "pierre":   ("fr", "fr_FR-upmc-medium", "fr/fr_FR/upmc/medium/", 1, "Pierre", False, 77),   # même fichier que Jessica
    "siwis":    ("fr", "fr_FR-siwis-medium", "fr/fr_FR/siwis/medium/", 0, "Siwis", True, 63),   # CC BY 4.0 (SIWIS, Univ. Édimbourg)
    "lj":       ("en", "en_US-ljspeech-high", "en/en_US/ljspeech/high/", 0, "LJ", True, 114),   # domaine public (LJ Speech)
    "kristin":  ("en", "en_US-kristin-medium", "en/en_US/kristin/medium/", 0, "Kristin", True, 64),   # domaine public (LibriVox)
    "john":     ("en", "en_US-john-medium", "en/en_US/john/medium/", 0, "John", False, 64),     # domaine public (LibriVox)
    "northern": ("en", "en_GB-northern_english_male-medium", "en/en_GB/northern_english_male/medium/", 0,
                 "Northern", False, 63),                                                       # CC BY-SA 4.0 (OpenSLR 83)
    "kaan":     ("tr", "tr_TR-dfki-medium", "tr/tr_TR/dfki/medium/", 0, "Kaan", False, 63),          # CC BY-NC-SA 4.0 (DFKI) : usage non commercial seulement
}
DEFAULT = {"fr": "jessica", "en": "lj", "tr": "kaan"}
CHOICE = dict(DEFAULT)   # langue -> id de voix choisie (rempli depuis config.json au démarrage)


def current_id(lang: str | None = None) -> str:
    lang = lang or i18n.LANG
    vid = CHOICE.get(lang)
    return vid if vid in VOICES and VOICES[vid][0] == lang else DEFAULT[lang]


def L_ready(name: str) -> str:
    return i18n.L(f"Ici {name}, je prends la radio.", f"{name} here, I've got the radio.", f"Ben {name}, telsiz bende.")


def engineer_name() -> str:
    return VOICES[current_id()][4]


def engineer_female() -> bool:
    return VOICES[current_id()][5]


def catalog(lang: str | None = None) -> list[dict]:
    """Voix proposées pour une langue, pour le menu du tableau de bord."""
    lang = lang or i18n.LANG
    return [{"id": k, "name": v[4], "female": v[5], "mb": v[6],
             "ready": (MODELS / f"{v[1]}.onnx").exists()}
            for k, v in VOICES.items() if v[0] == lang]
BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
SPEAKER = 0
SPEED = 0.92         # < 1 = plus rapide
RADIO_EFFECT = True


_TR_UNITS = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
_TR_UNITS_ORD = ["", "birinci", "ikinci", "üçüncü", "dördüncü", "beşinci", "altıncı", "yedinci", "sekizinci", "dokuzuncu"]
_TR_TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]
_TR_TENS_ORD = ["", "onuncu", "yirminci", "otuzuncu", "kırkıncı", "ellinci", "altmışıncı", "yetmişinci", "sekseninci", "doksanıncı"]


def ordinal_tr(n: int) -> str:
    """5 -> « beşinci », 12 -> « on ikinci », 120 -> « yüz yirminci » (1 à 999)."""
    if not 0 < n < 1000:
        return f"{n}."
    h, t, u = n // 100, n // 10 % 10, n % 10
    words = [] if not h else ["yüz"] if h == 1 else [_TR_UNITS[h], "yüz"]
    if u:
        words += [_TR_TENS[t], _TR_UNITS_ORD[u]]
    elif t:
        words.append(_TR_TENS_ORD[t])
    else:
        words[-1] = "yüzüncü"
    return " ".join(w for w in words if w)


def spoken(text: str) -> str:
    """Adapte le texte pour qu'il soit bien prononcé."""
    if i18n.en():
        text = text.replace("%", " percent").replace("°", " degrees")
    elif i18n.tr():
        text = re.sub(r"(\d)\.(\d)", r"\1,\2", text)      # 3.6 -> 3,6
        text = re.sub(r"(\d+(?:,\d+)?)\s?%", r"yüzde \1", text)   # 50 % -> yüzde 50
        text = text.replace("%", "yüzde ").replace("°", " derece")
        # « 5. virajda » -> « beşinci virajda » : sinon la voix s'arrête au point
        text = re.sub(r"\b(\d{1,3})\. (?=[a-zçğıöşü])", lambda m: ordinal_tr(int(m.group(1))) + " ", text)
    else:
        text = re.sub(r"(\d)\.(\d)", r"\1,\2", text)      # 3.6 -> 3,6
        text = text.replace("%", " pour cent").replace("°", " degrés")
    text = re.sub(r"\bP(\d+)\b", r"P \1", text)
    text = text.replace("!", ".")
    return text


def _download(url: str, dest: Path, progress=None) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  Téléchargement de la voix : {dest.name} …")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            if progress and total:
                progress(done * 100 // total)
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done * 100 // total:3d} %", end="", flush=True)
    print()
    tmp.replace(dest)


# ------------------------------------------------------------ prononciation (français)
_FEM = {"place", "seconde", "minute", "pénalité", "voiture", "fois", "position"}
_WORDS = ("tour", "seconde", "minute", "litre", "arrêt", "place", "pénalité", "degré", "dixième", "voiture", "position")
_PL_RE = re.compile(r"(?<![\d,.])(\d+(?:[.,]\d+)?)\s(" + "|".join(_WORDS) + r")s?\b")
_SPELL = [
    (re.compile(r"\bsetups\b", re.I), "sétups"), (re.compile(r"\bsetup\b", re.I), "sétup"),
    (re.compile(r"\bELMS\b"), "E L M S"),
    (re.compile(r"\bHypercars?\b", re.I), "Hyper car"),
    (re.compile(r"\bGT(\d)\b"), r"GT \1"), (re.compile(r"\bGTE\b"), "GT E"),
    (re.compile(r"\bLMDh\b", re.I), "L M D H"), (re.compile(r"\bLMH\b"), "L M H"),
]


def speakable_fr(text: str) -> str:
    """Texte affiché -> texte prononcé : accords (« 1 place » -> « une place », « 1 tours » -> « 1 tour »,
    « 3 minute » -> « 3 minutes »), virgule décimale, et mots que la voix lit mal (setup, ELMS, Hypercar…)."""
    t = re.sub(r"(\d)\.(\d)", r"\1,\2", text)

    def agree(m: re.Match) -> str:
        num, word = m.group(1), m.group(2)
        try:
            v = float(num.replace(",", "."))
        except ValueError:
            return m.group(0)
        if num == "1" and word in _FEM:
            return f"une {word}"
        return f"{num} {word}{'s' if v >= 2 else ''}"

    t = _PL_RE.sub(agree, t)
    for rx, rep in _SPELL:
        t = rx.sub(rep, t)
    return t


def _radio(pcm16: bytes, rate: int, volume: float = 1.3) -> bytes:
    """Effet radio : bande 300–3400 Hz, saturation douce, petit clic de micro."""
    import numpy as np

    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / rate)
    gain = np.clip((f - 200) / 150, 0, 1) * np.clip((3800 - f) / 600, 0, 1)
    x = np.fft.irfft(spec * gain, n=len(x))
    # volume : plus de "compression" radio + niveau plus haut (1.0 = ancien niveau, 1.6 = max)
    x = np.tanh(2.2 * volume * x / (np.max(np.abs(x)) or 1.0)) * min(0.99, 0.6 + 0.25 * volume)
    i = np.arange(int(rate * 0.012))
    click = 0.5 * np.sin(i * 0.9) * np.exp(-i / 40)
    gap = np.zeros(int(rate * 0.04))
    y = np.concatenate([click, gap, x, gap, click])
    return (np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes()


class Voice:
    def __init__(self) -> None:
        self.enabled = True
        self.quiet = False      # mode silence : seulement l'urgent (box, neutralisation, spotter, réponses)
        self.volume = 1.3
        self.engine = "aucune"
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._n = 0
        self._piper = None
        self.loaded = None          # (langue, id de voix) actuellement chargé
        self.downloading = None     # {"name": ..., "pct": ...} pendant un téléchargement
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------ chargement
    def _load(self) -> None:
        try:
            from piper import PiperVoice  # noqa
            MODELS.mkdir(parents=True, exist_ok=True)
            vid = current_id()
            _, name, folder, speaker, shown, _, _ = VOICES[vid]
            onnx, cfg = self._files(vid)
            if not (cfg.exists() and onnx.exists()):
                if self._piper is not None and self.loaded and self.loaded[0] == i18n.LANG:
                    # même langue : on garde la voix actuelle pendant le téléchargement de la nouvelle (en arrière-plan)
                    if not self.downloading:
                        threading.Thread(target=self._fetch, args=(vid,), daemon=True).start()
                    return
                self._fetch(vid)          # tout premier lancement : il faut bien une voix
            if self._piper is None or getattr(self, "_model", None) != name:
                self._piper = PiperVoice.load(onnx, config_path=cfg)
                self._model = name
            self.speaker = speaker
            self.loaded = (i18n.LANG, vid)
            self.engine = f"Piper · {shown}"
        except Exception as e:
            import traceback; traceback.print_exc()
            self.loaded = (i18n.LANG, current_id())   # pas de nouvel essai à chaque annonce
            if self._piper is None:
                print(f"  Voix Piper indisponible ({e!r}). Utilisation de la voix Windows.")
                self.engine = "Windows" if sys.platform == "win32" else "aucune"
            else:
                print(f"  Nouvelle voix indisponible ({e!r}), on garde la voix actuelle.")
        print(f"  Voix : {self.engine}")

    @staticmethod
    def _files(vid: str) -> tuple[Path, Path]:
        name = VOICES[vid][1]
        return MODELS / f"{name}.onnx", MODELS / f"{name}.onnx.json"

    def _fetch(self, vid: str) -> None:
        """Télécharge les fichiers d'une voix (une seule fois)."""
        _, name, folder, _, shown, _, _ = VOICES[vid]
        url = BASE_URL + folder + name
        onnx, cfg = self._files(vid)
        self.downloading = {"name": shown, "pct": 0}
        try:
            if not cfg.exists():
                _download(url + ".onnx.json", cfg)
            if not onnx.exists():
                _download(url + ".onnx", onnx, lambda pct: self.downloading and self.downloading.update(pct=pct))
        except Exception as e:
            print(f"  Téléchargement de la voix {shown} impossible : {e!r}")
            self.loaded = (i18n.LANG, vid)   # on n'insiste pas
        finally:
            self.downloading = None
        if threading.current_thread() is not threading.main_thread() and self._piper is not None:
            self.say(L_ready(shown), urgent=True)   # elle se présente dès qu'elle est prête

    # ------------------------------------------------------------ API
    def choose(self, vid: str) -> bool:
        """Change de voix (pour sa langue). Elle est chargée (et téléchargée si besoin) avant la prochaine annonce."""
        if vid not in VOICES:
            return False
        CHOICE[VOICES[vid][0]] = vid
        return True

    def flush(self) -> None:
        """Vide la file d'attente (changement de langue)."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def say(self, text: str, urgent: bool = False, spot: bool = False) -> None:
        """spot=True : message du spotter (« à gauche »), passe devant tout et est jeté s'il arrive trop tard."""
        if not self.enabled or (self.quiet and not urgent and not spot):
            return
        self._n += 1
        import time as _t
        self._q.put((-1 if spot else 0 if urgent else 1, self._n, text, _t.monotonic(), spot))

    # ------------------------------------------------------------ rendu
    def _render_piper(self, text: str) -> bytes:
        from piper import SynthesisConfig
        cfg = SynthesisConfig(speaker_id=getattr(self, "speaker", SPEAKER), length_scale=SPEED)
        pcm = bytearray()
        rate = 22050
        if i18n.fr():
            text = speakable_fr(text)
        for chunk in self._piper.synthesize(text, syn_config=cfg):
            pcm += chunk.audio_int16_bytes
            rate = chunk.sample_rate
        data = _radio(bytes(pcm), rate, self.volume) if RADIO_EFFECT else bytes(pcm)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(data)
        return buf.getvalue()

    def _play(self, wav: bytes) -> None:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(wav, winsound.SND_MEMORY)
        else:  # tests hors Windows : on enregistre le fichier
            (LOCAL / "derniere_annonce.wav").write_bytes(wav)

    def _speak_windows(self, text: str) -> None:
        safe = text.replace("'", "''")
        ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$v=$s.GetInstalledVoices()|?{{$_.VoiceInfo.Culture.Name -like '{i18n.LANG}*'}}|select -First 1; "
              "if($v){$s.SelectVoice($v.VoiceInfo.Name)}; $s.Rate=1; "
              f"$s.Speak('{safe}')")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode:
            print(f"  Erreur voix Windows : {r.stderr.strip()[:300]}")

    def _worker(self) -> None:
        self._load()
        while True:
            _, _, text, t0, spot = self._q.get()
            import time as _t
            if self.loaded != (i18n.LANG, current_id()):   # langue ou voix changée : on charge l'autre voix
                self._load()
            if spot and _t.monotonic() - t0 > 1.2:
                continue   # info du spotter périmée : on ne la dit pas
            print(f"  [radio] {text}")
            if not self.enabled:
                continue
            try:
                t = spoken(text)
                if self._piper is not None:
                    self._play(self._render_piper(t))
                elif self.engine == "Windows":
                    self._speak_windows(t)
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"  Erreur voix : {e!r}")
