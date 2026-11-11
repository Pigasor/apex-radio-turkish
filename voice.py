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
BUNDLED = HERE / "bundled"   # fournie avec l'installateur (pas « models » : paths.migrate() le déplacerait)
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
    # turc : Ceren et Kaan parlent avec les voix neuronales de Microsoft (en ligne, voir ONLINE) ;
    # sans internet, c'est la voix Piper indiquée ici (DFKI, CC BY-NC-SA 4.0 : usage non commercial) qui prend le relais
    "ceren":    ("tr", "tr_TR-dfki-medium", "tr/tr_TR/dfki/medium/", 0, "Ceren", True, 63),
    "kaan":     ("tr", "tr_TR-dfki-medium", "tr/tr_TR/dfki/medium/", 0, "Kaan", False, 63),
    "kaan_cd":  ("tr", "tr_TR-dfki-medium", "tr/tr_TR/dfki/medium/", 0, "Kaan", False, 63),   # « Kaan · çevrimdışı » dans le menu
}
# id -> (voix Microsoft, débit, hauteur). Service de lecture à voix haute d'Edge (paquet edge-tts) : il faut internet,
# et le texte des messages part chez Microsoft. Pas une API officielle : si elle ne répond plus, Piper prend le relais.
ONLINE = {"ceren": ("tr-TR-EmelNeural", "+5%", "+10Hz"), "kaan": ("tr-TR-AhmetNeural", "+8%", "+0Hz")}
# préparés dès le chargement de la voix en ligne : spotter d'abord, puis les messages urgents au texte fixe
PREWARM_TR = ("Üç araç yan yana!", "Solda araç!", "Sağda araç!", "Temiz.", "Tüm pistte sarı bayrak! Yavaşla.",
              "Sarı bayrak bitti, yarış devam ediyor.", "Damalı bayrak! Tebrikler.", "Bu tur box.",
              "Dikkat, lastikler ve frenler soğuk. İlk frenajda yavaş.",
              "Lastikler ve frenler ısındı. Hadi, artık bastırabilirsin!",
              "Cezayı çekmek için son tur, bu turun sonunda pite gir!", "Araçtan bir parça koptu.")
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
    return [{"id": k, "name": v[4], "female": v[5], "mb": v[6], "online": k in ONLINE,
             "label": v[4] + (" · çevrimdışı" if k.endswith("_cd") else ""),
             "ready": k in ONLINE or (MODELS / f"{v[1]}.onnx").exists() or (BUNDLED / f"{v[1]}.onnx").exists()}
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


# ------------------------------------------------------------ prononciation (turc)
# eSpeak met l'accent turc sur la dernière syllabe. C'est faux pour certains mots courants (« HAdi », « HArika »),
# pour le présent en -iyor (« kazaNIyorsun ») et pour l'impératif négatif (« bıRAKma »).
# Mot (sans accent graphique, minuscules) -> syllabe accentuée, comptée depuis 1.
_TR_STRESS = {
    "hadi": 1, "haydi": 1, "harika": 1, "harikasın": 1, "şimdi": 1, "sonra": 1, "nasıl": 1, "belki": 1, "biraz": 1,
    "yani": 1, "işte": 1, "hemen": 1, "henüz": 1, "artık": 1, "önce": 1, "aynı": 1, "yalnızca": 1, "kimse": 1,
    "nerede": 1, "evet": 1, "hayır": 1, "merhaba": 2, "lütfen": 1, "süper": 1, "bravo": 1, "yine": 1, "ancak": 1,
    "hâlâ": 1, "hala": 1, "çünkü": 1, "sakın": 1, "yavaş": 1, "aferin": 1, "tamamen": 2, "gerçekten": 2,
    "bırakma": 2, "etme": 1, "verme": 1, "yapma": 1, "kaçırma": 2, "zorlama": 2, "unutma": 2, "durma": 1,
}
_TR_VOWELS = set("aeiouyæɑɐəɛɪɔʊœøɯʏ")


def _vowel_starts(ph: str) -> list[int]:
    """Position de la voyelle de chaque syllabe : la voix a appris l'accent juste avant la voyelle (« hˈadɪ »)."""
    return [i for i, c in enumerate(ph) if c in _TR_VOWELS and (i == 0 or ph[i - 1] not in _TR_VOWELS)]


def _restress(ph: str, syllable: int) -> str:
    bare = ph.replace("ˈ", "")
    st = _vowel_starts(bare)
    if not 1 <= syllable <= len(st):
        return ph
    k = st[syllable - 1]
    return bare[:k] + "ˈ" + bare[k:]


def _tr_word_stress(word: str, ph: str) -> str:
    w = word.lower().replace("i̇", "i")
    if w in _TR_STRESS:
        return _restress(ph, _TR_STRESS[w])
    m = re.search(r"(.*?[aeıioöuü])yor(um|sun|uz|sunuz|lar|du|muş)?$", w)
    if m and "ˈ" in ph:   # présent : accent sur la voyelle juste avant -yor
        n = sum(1 for c in m.group(1) if c in "aeıioöuü")
        return _restress(ph, n)
    return ph


def tr_stress(pv) -> None:
    """Corrige l'accent des mots turcs dans la phonémisation de Piper (une seule fois par voix chargée)."""
    if getattr(pv, "_tr_stress", False):
        return
    original = pv.phonemize

    def phonemize(text: str) -> list[list[str]]:
        words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", text)
        fixes = {}
        for w in {w.lower() for w in words}:
            parts = original(w)
            if len(parts) != 1:
                continue
            ph = "".join(parts[0])
            new = _tr_word_stress(w, ph)
            if new != ph:
                fixes[ph] = new
        out = []
        for sent in original(text):
            s = "".join(sent)
            for old, new in sorted(fixes.items(), key=lambda x: -len(x[0])):
                s = re.sub(r"(?<![^\s,.!?;:])" + re.escape(old) + r"(?![^\s,.!?;:])", new, s)
            out.append(list(s))
        return out

    pv.phonemize = phonemize
    pv._tr_stress = True


# mots étrangers écrits comme ils se prononcent en turc
_TR_SPELL = [
    (re.compile(r"\bsetup", re.I), "setap"), (re.compile(r"\bundercut", re.I), "andırkat"),
    (re.compile(r"\bovercut", re.I), "ovırkat"), (re.compile(r"\bslick", re.I), "slik"),
    (re.compile(r"\bspotter", re.I), "spotır"), (re.compile(r"\bHypercar", re.I), "hayperkar"),
    (re.compile(r"\bELMS\b"), "E L M S"), (re.compile(r"\bWEC\b"), "vek"),
    (re.compile(r"\bGT(\d)\b"), r"Ge Te \1"), (re.compile(r"\bGTE\b"), "Ge Te E"),
    (re.compile(r"\bLMP(\d)\b"), r"L M P \1"), (re.compile(r"\bLMU\b"), "L M U"),
]


def spoken(text: str, keep_excl: bool = False) -> str:
    """Adapte le texte pour qu'il soit bien prononcé."""
    if i18n.en():
        text = text.replace("%", " percent").replace("°", " degrees")
    elif i18n.tr():
        text = re.sub(r"(\d)\.(\d)", r"\1,\2", text)      # 3.6 -> 3,6
        text = re.sub(r"(\d+(?:,\d+)?)\s?%", r"yüzde \1", text)   # 50 % -> yüzde 50
        text = text.replace("%", "yüzde ").replace("°", " derece")
        # « 5. virajda » -> « beşinci virajda » : sinon la voix s'arrête au point
        text = re.sub(r"\b(\d{1,3})\. (?=[a-zçğıöşü])", lambda m: ordinal_tr(int(m.group(1))) + " ", text)
        for rx, rep in _TR_SPELL:
            text = rx.sub(rep, text)
    else:
        text = re.sub(r"(\d)\.(\d)", r"\1,\2", text)      # 3.6 -> 3,6
        text = text.replace("%", " pour cent").replace("°", " degrés")
    text = re.sub(r"\bP(\d+)\b", r"P \1", text)
    if not keep_excl:
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
        self._cache: dict = {}      # (voix en ligne, texte) -> (pcm16, débit) : phrases déjà reçues
        self._offline_until = 0.0   # voix en ligne injoignable : Piper jusqu'à cette heure
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
            if vid in ONLINE:
                self.engine = f"Microsoft · {shown}"
                # les phrases du spotter doivent partir tout de suite : on les prépare dès maintenant
                threading.Thread(target=self._prewarm, args=(vid,), daemon=True).start()
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
        d = BUNDLED if (BUNDLED / f"{name}.onnx").exists() and (BUNDLED / f"{name}.onnx.json").exists() else MODELS
        return d / f"{name}.onnx", d / f"{name}.onnx.json"

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
        if i18n.tr():
            # moins d'aléa sur la durée des sons : sinon la voix turque étire des voyelles au hasard (« haaadi »)
            cfg = SynthesisConfig(speaker_id=cfg.speaker_id, length_scale=SPEED, noise_scale=0.5, noise_w_scale=0.35)
            tr_stress(self._piper)
        for chunk in self._piper.synthesize(text, syn_config=cfg):
            pcm += chunk.audio_int16_bytes
            rate = chunk.sample_rate
        data = _radio(bytes(pcm), rate, self.volume) if RADIO_EFFECT else bytes(pcm)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(data)
        return buf.getvalue()

    def _online_pcm(self, vid: str, text: str, timeout: float = 8.0) -> tuple[bytes, int]:
        """Texte -> PCM 16 bits mono avec la voix Microsoft de vid (mis en cache)."""
        key = (vid, text)
        if key in self._cache:
            return self._cache[key]
        import asyncio
        import edge_tts
        name, rate, pitch = ONLINE[vid]

        async def fetch() -> bytes:
            buf = bytearray()
            async for ch in edge_tts.Communicate(text, name, rate=rate, pitch=pitch).stream():
                if ch["type"] == "audio":
                    buf += ch["data"]
            return bytes(buf)

        mp3 = asyncio.run(asyncio.wait_for(fetch(), timeout))
        if not mp3:
            raise ConnectionError("pas d'audio")
        import av
        rs = av.AudioResampler(format="s16", layout="mono", rate=24000)
        with av.open(io.BytesIO(mp3)) as c:
            pcm = b"".join(bytes(f.planes[0])[: f.samples * 2] for fr in c.decode(audio=0) for f in rs.resample(fr))
        out = (pcm, 24000)
        if len(self._cache) > 300:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = out
        return out

    def _quiet_fetch(self, vid: str, text: str, timeout: float = 20.0) -> bool:
        try:
            self._online_pcm(vid, text, timeout)
            return True
        except Exception as e:
            print(f"  Voix en ligne : {text!r} pas reçu ({e!r})")
            return False

    def _prewarm(self, vid: str) -> None:
        import time as _t
        for msg in PREWARM_TR:
            for attempt in range(3):   # le 1er contact avec le service peut être lent
                if current_id() != vid or self._quiet_fetch(vid, spoken(msg, keep_excl=True)):
                    break
                _t.sleep(5)

    def _render_online(self, vid: str, text: str, spot: bool, urgent: bool = False) -> bytes | None:
        """None : pas de voix en ligne (hors connexion, trop lent, ou phrase du spotter pas encore prête)."""
        import time as _t
        t = spoken(text, keep_excl=True)
        if (vid, t) not in self._cache:
            if spot or _t.monotonic() < self._offline_until:
                if spot:   # pas d'attente pour le spotter : Piper maintenant, la voix en ligne la prochaine fois
                    threading.Thread(target=self._quiet_fetch, args=(vid, t), daemon=True).start()
                return None
            # le service répond en ~2 à 4 s, parfois bien plus : un message urgent n'attend pas plus de 5 s
            limit = 5.0 if urgent else 10.0
            res: dict = {}

            def fetch() -> None:
                try:
                    self._online_pcm(vid, t, 30.0)   # finit en arrière-plan : servira la prochaine fois
                except Exception as e:
                    res["err"] = e
            th = threading.Thread(target=fetch, daemon=True)
            th.start()
            start = _t.monotonic()
            while th.is_alive() and _t.monotonic() - start < limit:
                th.join(0.1)
                self._serve_spotter()     # « à gauche » ne doit pas attendre la voix en ligne
            if "err" in res:
                self._offline_until = _t.monotonic() + 60
                print(f"  Voix en ligne injoignable ({res['err']!r}) : voix hors ligne pendant une minute.")
                return None
            if (vid, t) not in self._cache:
                print(f"  Voix en ligne trop lente (> {limit:.0f} s) : voix hors ligne pour ce message.")
                return None
        pcm, rate = self._cache[(vid, t)]
        data = _radio(pcm, rate, self.volume) if RADIO_EFFECT else pcm
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(data)
        return buf.getvalue()

    def _serve_spotter(self) -> None:
        """Pendant l'attente de la voix en ligne : dit tout de suite un message du spotter arrivé entre-temps."""
        import time as _t
        try:
            item = self._q.get_nowait()
        except queue.Empty:
            return
        if item[0] != -1:            # pas du spotter : on le remet à sa place
            self._q.put(item)
            return
        _, _, text, t0, _ = item
        if _t.monotonic() - t0 > 1.2 or not self.enabled:
            return
        print(f"  [radio] {text}")
        vid = current_id()
        t = spoken(text, keep_excl=True)
        if (vid, t) in self._cache:
            pcm, rate = self._cache[(vid, t)]
            self._play_pcm(_radio(pcm, rate, self.volume) if RADIO_EFFECT else pcm, rate)
        elif self._piper is not None:
            self._play(self._render_piper(spoken(text)))

    def _play_pcm(self, pcm: bytes, rate: int) -> None:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(pcm)
        self._play(buf.getvalue())

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
            prio, _, text, t0, spot = self._q.get()
            import time as _t
            if self.loaded != (i18n.LANG, current_id()):   # langue ou voix changée : on charge l'autre voix
                self._load()
            if spot and _t.monotonic() - t0 > 1.2:
                continue   # info du spotter périmée : on ne la dit pas
            print(f"  [radio] {text}")
            if not self.enabled:
                continue
            try:
                vid = current_id()
                wav = self._render_online(vid, text, spot, urgent=prio == 0) if vid in ONLINE else None
                if wav:
                    self._play(wav)
                    continue
                t = spoken(text)
                if self._piper is not None:
                    self._play(self._render_piper(t))
                elif self.engine == "Windows":
                    self._speak_windows(t)
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"  Erreur voix : {e!r}")
