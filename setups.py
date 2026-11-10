"""
Atelier setup (partie 3) : setups sur mesure par circuit.

- Lit les fichiers .svm de LMU (dossier UserData\\player\\Settings\\<circuit>).
- Apprend, voiture par voiture, à quoi correspond chaque cran (index ↔ valeur
  affichée) à partir de TOUS tes fichiers, pour savoir dans quel sens tourner.
- Transforme les problèmes détectés en piste (behavior.py) en changements
  précis : "barre antiroulis avant : D18 H-H -> D18 S-H".
- Crée une NOUVELLE version ("APEX <circuit> v2.svm") : les fichiers existants
  ne sont jamais modifiés.
- Garde l'historique des versions et le meilleur tour fait avec chacune.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path

from i18n import L

HERE = Path(__file__).parent
from paths import DATA, LOCAL  # noqa: E402
HISTORY = DATA / "setups_history.json"
BACKUPS = DATA / "setup_backups"   # anciennes versions, gardées hors du dossier LMU
DEFAULT_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Le Mans Ultimate\UserData\player\Settings"
LMU_APPID = 2399420
WHEEL_SECTIONS = ["FRONTLEFT", "FRONTRIGHT", "REARLEFT", "REARRIGHT"]
FIXED = ("n/a", "non-adjustable", "non adjustable")

# Pour reconnaître le dossier de setup à partir du nom de circuit donné par le jeu
ALIASES = {
    "lemans": ["sarthe", "le mans"], "paulricard": ["ricard", "castellet"], "daytonarc": ["daytona"],
    "lagunaseca": ["laguna"], "longbeach": ["long beach"], "circuitoftheamericas": ["americas", "cota"],
    "circuitdebarcelona": ["barcelona", "catalunya"], "imola": ["imola", "enzo"], "interlagos": ["interlagos", "pace"],
    "portimao": ["portimao", "algarve"], "bahrain": ["bahrain", "sakhir"], "spa": ["spa", "francorchamps"],
    "qatar": ["lusail", "qatar", "losail"], "sebring": ["sebring"], "fuji": ["fuji"], "monza": ["monza"],
}


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if c.isalnum() or c == " ")


def _num(display: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", display)
    if not m:
        return None
    v = float(m.group())
    if " mm" in display and "cm" not in display:
        v /= 10
    return v


LABELS = {
    "RearBrakeSetting": "Répartition de freinage", "FrontAntiSwaySetting": "Barre antiroulis avant",
    "RearAntiSwaySetting": "Barre antiroulis arrière", "FWSetting": "Aileron avant", "RWSetting": "Aileron arrière",
    "TractionControlMapSetting": "Contrôle de traction", "DiffPowerSetting": "Différentiel (accélération)",
    "DiffCoastSetting": "Différentiel (lever de pied)", "CamberSetting": "Carrossage", "PressureSetting": "Pression",
    "RideHeightSetting": "Hauteur de caisse", "AntilockBrakeSystemMapSetting": "ABS",
}
SECTION_FR = {"FRONTLEFT": "AVG", "FRONTRIGHT": "AVD", "REARLEFT": "ARG", "REARRIGHT": "ARD"}
LABELS_EN = {
    "RearBrakeSetting": "Brake bias", "FrontAntiSwaySetting": "Front anti-roll bar",
    "RearAntiSwaySetting": "Rear anti-roll bar", "FWSetting": "Front wing", "RWSetting": "Rear wing",
    "TractionControlMapSetting": "Traction control", "DiffPowerSetting": "Diff (power)",
    "DiffCoastSetting": "Diff (coast)", "CamberSetting": "Camber", "PressureSetting": "Pressure",
    "RideHeightSetting": "Ride height", "AntilockBrakeSystemMapSetting": "ABS",
}
SECTION_EN = {"FRONTLEFT": "FL", "FRONTRIGHT": "FR", "REARLEFT": "RL", "REARRIGHT": "RR"}
LABELS_TR = {
    "RearBrakeSetting": "Fren dengesi", "FrontAntiSwaySetting": "Ön viraj demiri",
    "RearAntiSwaySetting": "Arka viraj demiri", "FWSetting": "Ön kanat", "RWSetting": "Arka kanat",
    "TractionControlMapSetting": "Çekiş kontrolü", "DiffPowerSetting": "Diferansiyel (güç)",
    "DiffCoastSetting": "Diferansiyel (boşta)", "CamberSetting": "Kamber", "PressureSetting": "Basınç",
    "RideHeightSetting": "Sürüş yüksekliği", "AntilockBrakeSystemMapSetting": "ABS",
}
SECTION_TR = {"FRONTLEFT": "ön sol", "FRONTRIGHT": "ön sağ", "REARLEFT": "arka sol", "REARRIGHT": "arka sağ"}


def label(section: str, key: str) -> str:
    l = L(LABELS.get(key, key), LABELS_EN.get(key, key), LABELS_TR.get(key))
    sec = L(SECTION_FR.get(section, ""), SECTION_EN.get(section, ""), SECTION_TR.get(section, ""))
    return f"{l} {sec}" if section in SECTION_FR else l


# ===================================================================== fichiers
class Setup:
    """Un fichier .svm : on garde les lignes telles quelles pour pouvoir le réécrire."""

    LINE = re.compile(r"^(\w+)=(-?\d+)(//(.*))?$")

    def __init__(self, path: Path) -> None:
        self.path = path
        raw = path.read_bytes().decode("utf-8", errors="replace")
        self.newline = "\r\n" if "\r\n" in raw else "\n"
        self.lines = raw.splitlines()
        self.vehicle = ""
        self.values = {}      # (section, clé) -> (index, affichage, n° de ligne)
        section = ""
        for n, line in enumerate(self.lines):
            if line.startswith("VehicleClassSetting="):
                self.vehicle = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            else:
                m = self.LINE.match(line.strip())
                if m and section:
                    self.values[(section, m.group(1))] = (int(m.group(2)), (m.group(4) or "").strip(), n)

    def get(self, section: str, key: str):
        return self.values.get((section, key))

    def write_copy(self, dest: Path, changes: list[dict], note: str, overwrite: bool = False) -> None:
        lines = list(self.lines)
        for c in changes:
            idx, disp, n = self.values[(c["section"], c["key"])]
            shown = c["to_disp"] if not str(c["to_disp"]).startswith("cran") else f"APEX (avant : {c['from_disp']})"
            lines[n] = f"{c['key']}={c['to']}//{shown}"
        for i, line in enumerate(lines):
            if line.startswith("Notes="):
                lines[i] = 'Notes="' + note.replace('"', "'") + '"'
                break
        text = self.newline.join(lines) + self.newline
        if not overwrite:
            with open(dest, "x", encoding="utf-8", newline="") as f:   # "x" : jamais d'écrasement
                f.write(text)
            return
        tmp = dest.with_name(dest.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, dest)

    def is_apex(self) -> bool:
        """Fichier créé par Apex Radio (on n'a le droit de modifier que ceux-là)."""
        return any(l.startswith('Notes="Apex Radio') for l in self.lines)


def FACTORY_MSG() -> str:
    return L("Tu roules avec le setup d'usine : il n'existe pas en fichier, donc Apex ne peut pas le modifier. "
             "Dans le garage de LMU, clique sur Enregistrer et donne-lui un nom (par exemple « Base »). "
             "Apex le détectera tout seul et pourra créer le setup modifié.",
             "You're driving the default setup: it isn't saved as a file, so Apex can't change it. "
             "In the LMU garage, click Save and give it a name (for example \"Base\"). "
             "Apex will pick it up automatically and can then create the modified setup.",
             "Fabrika setup'ıyla sürüyorsun: dosya olarak kayıtlı olmadığı için Apex onu değiştiremez. " "LMU garajında Kaydet'e tıkla ve bir ad ver (örneğin \"Base\"). " "Apex onu kendiliğinden algılar ve değiştirilmiş setup'ı oluşturabilir.")


# ===================================================================== voiture
KNOWN_CLASSES = {"HYPERCAR", "LMP2", "LMP2_ELMS", "LMP3", "GTE", "GT3", "LMGT3"}


def series_tag(car_class: str, car: str = "") -> str:
    """« ELMS » ou « WEC » : la LMP2 existe en deux versions dans LMU (LMP2 et LMP2_ELMS)."""
    c, v = (car_class or "").upper(), (car or "").upper()
    if "ELMS" in c or ":ELMS" in v:
        return "ELMS"
    if c == "LMP2":
        return "WEC"
    return ""


def setup_class(vehicle: str) -> str:
    """Catégorie écrite dans un fichier .svm : VehicleClassSetting="ELMS2025 LMP2_ELMS Oreca_07" -> LMP2_ELMS."""
    for tok in (vehicle or "").replace('"', " ").split():
        if tok.upper() in KNOWN_CLASSES:
            return tok.upper()
    return ""


def find_settings_dir() -> str:
    """Dossier des setups de LMU, où que soit installé le jeu : bibliothèques Steam
    (libraryfolders.vdf, appmanifest de LMU), puis emplacements courants, sinon l'emplacement par défaut."""
    import os
    import re as _re
    cands: list[Path] = []
    roots: list[Path] = []
    try:
        import winreg
        for hive, key, name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(Path(str(winreg.QueryValueEx(k, name)[0])))
            except OSError:
                pass
    except Exception:
        pass
    roots.append(Path(r"C:\Program Files (x86)\Steam"))
    libs: list[Path] = []
    for r in roots:
        libs.append(r)
        try:
            vdf = (r / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="ignore")
            libs += [Path(m.replace("\\\\", "\\")) for m in _re.findall(r'"path"\s+"([^"]+)"', vdf)]
        except OSError:
            pass
    for lib in libs:
        sa = lib / "steamapps"
        name = "Le Mans Ultimate"
        try:
            acf = (sa / f"appmanifest_{LMU_APPID}.acf").read_text(encoding="utf-8", errors="ignore")
            m = _re.search(r'"installdir"\s+"([^"]+)"', acf)
            if m:
                name = m.group(1)
        except OSError:
            pass
        cands.append(sa / "common" / name / "UserData" / "player" / "Settings")
    for d in "CDEFGH":
        for base in (r"SteamLibrary", r"Steam", r"Program Files (x86)\Steam", r"Program Files\Steam", r"Games\Steam", r"Jeux\Steam"):
            cands.append(Path(f"{d}:\\") / base / "steamapps" / "common" / "Le Mans Ultimate" / "UserData" / "player" / "Settings")
    seen = set()
    for c in cands:
        k = str(c).lower()
        if k in seen:
            continue
        seen.add(k)
        try:
            if c.is_dir():
                return str(c)
        except OSError:
            continue
    return DEFAULT_DIR


# ===================================================================== atelier
class Workshop:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        own = cfg.get("setups_dir")
        self.dir = Path(own) if own and Path(own).is_dir() else Path(find_settings_dir())
        try:
            self.history = json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception:
            self.history = {}
        self.catalog = {}     # (véhicule, section, clé) -> {index: affichage}
        self.mode = "Course"  # "Qualif" ou "Course" : un fichier APEX pour chacun
        self._scanned = 0.0
        self.factory = False      # LMU roule avec le setup d'usine (pas de fichier à copier)
        self._last_active = None
        self.car_class = ""       # catégorie de la voiture en piste (LMP2, LMP2_ELMS…), mise à jour par app.py
        self.car = ""
        self.mismatch = None      # dernier écart setup / voiture détecté (pour le tableau de bord)

    # ----------------------------------------------------------- inventaire
    def ok(self) -> bool:
        return self.dir.is_dir()

    def scan(self, force: bool = False) -> None:
        if not force and time.time() - self._scanned < 30:
            return
        self._scanned = time.time()
        self.catalog = {}
        if not self.ok():
            return
        for f in self.dir.glob("*/*.svm"):
            try:
                s = Setup(f)
            except Exception:
                continue
            for (sec, key), (idx, disp, _) in s.values.items():
                if disp and not disp.startswith("APEX") and not re.match(r"^(V\d|index)", disp):
                    self.catalog.setdefault((s.vehicle, sec, key), {})[idx] = disp

    def folders(self) -> list[dict]:
        if not self.ok():
            return []
        out = []
        for d in sorted(p for p in self.dir.iterdir() if p.is_dir()):
            files = sorted(f.name for f in d.glob("*.svm"))
            if files:
                out.append({"name": d.name, "files": files})
        return out

    def guess_folder(self, track: str) -> str | None:
        if not track or not self.ok():
            return None
        t = _norm(track)
        for d in self.dir.iterdir():
            if not d.is_dir():
                continue
            key = _norm(d.name).replace(" ", "")
            words = ALIASES.get(key, [_norm(d.name)])
            if any(w in t for w in words) or key in t.replace(" ", ""):
                return d.name
        return None

    # ----------------------------------------------------------- état
    def set_mode(self, mode: str) -> None:
        if mode in ("Qualif", "Course"):
            self.mode = mode

    def tag(self) -> str:
        return series_tag(self.car_class, self.car)

    def _hkey(self, folder: str | None) -> str:
        if not folder:
            return ""
        t = self.tag()
        return f"{folder} · {t} · {self.mode}" if t else f"{folder} · {self.mode}"

    def apex_name(self, folder: str, base: "Setup | None" = None) -> str:
        """Nom du fichier APEX. La version (ELMS / WEC) est celle du setup de départ, pas celle de la voiture :
        un setup WEC reste un setup WEC, même créé pendant qu'on roule en LMP2 ELMS."""
        t = self.tag()
        if base is not None:
            sc = setup_class(base.vehicle)
            if sc:
                t = series_tag(sc)
        return f"APEX {folder} {t} {self.mode}.svm" if t else f"APEX {folder} {self.mode}.svm"

    def class_mismatch(self, track: str) -> dict | None:
        """Le setup choisi a été fait pour une autre version de la voiture (ex. LMP2 standard sur la LMP2 ELMS) ?"""
        st = self.state(track)
        car = (self.car_class or "").upper()
        if not st["folder"] or not st["current"] or car not in KNOWN_CLASSES:
            return None
        try:
            sc = setup_class(Setup(self.dir / st["folder"] / st["current"]).vehicle)
        except Exception:
            return None
        if not sc or sc == car:
            return None
        return {"file": st["current"], "setup": sc, "car": car}

    def state(self, track: str) -> dict:
        folder = self.cfg.get("setup_folder_for", {}).get(track) or self.guess_folder(track)
        h = self.history.get(self._hkey(folder), {})
        return {"ok": self.ok(), "dir": str(self.dir), "track": track, "folder": folder, "mode": self.mode,
                "current": h.get("current"), "label": self._label(h), "apex_file": h.get("apex_file"),
                "apex_label": h.get("apex_label"), "versions": h.get("versions", []), "results": h.get("results", {}),
                "factory": self.factory, "series": self.tag(), "mismatch": self.mismatch}

    @staticmethod
    def _label(h: dict) -> str | None:
        """Nom de la version en cours : le nom du fichier, ou "v3" pour le fichier APEX."""
        cur = h.get("current")
        if cur and cur == h.get("apex_file"):
            return h.get("apex_label")
        return cur

    def select(self, track: str, folder: str, file: str) -> None:
        self.cfg.setdefault("setup_folder_for", {})[track] = folder
        h = self.history.setdefault(self._hkey(folder), {"versions": [], "results": {}})
        h["current"] = file
        self.factory = False      # un vrai fichier est choisi (par toi ou détecté dans LMU)
        self._save()

    def _save(self) -> None:
        HISTORY.write_text(json.dumps(self.history, indent=2, ensure_ascii=False), encoding="utf-8")

    def record_lap(self, track: str, lap: float, issues: list) -> None:
        """Note le tour valide dans les résultats du setup en cours."""
        st = self.state(track)
        if not st["folder"] or not st["current"]:
            return
        if not st["label"]:
            return
        r = self.history.setdefault(self._hkey(st["folder"]), {"versions": [], "results": {}}).setdefault("results", {}).setdefault(st["label"], {"best": None, "laps": 0})
        r["laps"] += 1
        if r["best"] is None or lap < r["best"]:
            r["best"] = lap
        r["issues"] = [i["text"] for i in issues[:4]]
        self._save()

    def sync_active(self, track: str) -> str | None:
        """Lit dans LMU le setup chargé ("Paulricard\\Nom") et le sélectionne tout seul. Renvoie son nom s'il a changé."""
        import urllib.request as _u
        try:
            op = _u.build_opener(_u.ProxyHandler({}))
            with op.open("http://localhost:6397/rest/garage/summary", timeout=2) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None
        active = (j or {}).get("activeSetup") or ""
        if active != self._last_active:
            self._last_active = active
            # pas de « Circuit\\Nom » : c'est le setup d'usine, il n'existe pas en fichier
            self.factory = "\\" not in active
        if "\\" not in active:
            return None
        folder, name = active.split("\\", 1)
        file = name if name.endswith(".svm") else name + ".svm"
        if not (self.dir / folder / file).exists():
            return None
        st = self.state(track)
        if st["folder"] == folder and st["current"] == file:
            return None
        had = bool(st["current"])
        self.select(track, folder, file)
        print(f"  Setup chargé dans LMU détecté : {file}")
        return file if had else None   # 1re détection : l'analyse en cours porte déjà sur ce setup, on la garde

    # ----------------------------------------------------------- crans
    def _step(self, setup: Setup, section: str, key: str, delta: int):
        """Index + delta, avec l'affichage attendu si on l'a déjà vu dans un de tes fichiers."""
        cur = setup.get(section, key)
        if cur is None:
            return None
        idx, disp, _ = cur
        known = self.catalog.get((setup.vehicle, section, key), {})
        if disp.startswith("APEX"):
            disp = known.get(idx, f"cran {idx}")
        if not disp or disp.lower() in FIXED or any(f in disp.lower() for f in FIXED):
            return None
        if len(known) <= 1 and _num(disp) is None:
            return None   # réglage peut-être fixe sur cette voiture : on n'y touche pas
        new = idx + delta
        if new < 0:
            return None
        if known and new > max(known) + 3:
            return None
        return {"section": section, "key": key, "label": label(section, key), "from": idx, "to": new, "from_disp": disp,
                "to_disp": known.get(new, f"cran {new}")}

    def _direction(self, setup: Setup, section: str, key: str, default: int) -> int:
        """+1 si monter l'index fait monter la valeur affichée, -1 sinon (appris de tes fichiers)."""
        known = self.catalog.get((setup.vehicle, section, key), {})
        pts = sorted((i, _num(d)) for i, d in known.items() if _num(d) is not None)
        if len(pts) >= 2:
            return 1 if pts[-1][1] > pts[0][1] else -1
        return default

    # ----------------------------------------------------------- propositions
    def _live(self, s: Setup, live: dict) -> list[dict]:
        """Réglages changés dans la voiture pendant la session -> mêmes valeurs dans le fichier."""
        out = []
        why = L("réglage fait en piste", "set on track", "pistte ayarlandı")
        bf = live.get("bias_front")
        cur = s.get("CONTROLS", "RearBrakeSetting")
        if bf and cur:
            idx, disp, _ = cur
            known = self.catalog.get((s.vehicle, "CONTROLS", "RearBrakeSetting"), {})
            pts = {i: _num(d) for i, d in known.items() if _num(d) is not None}
            if _num(disp) is not None and not disp.startswith("APEX"):
                pts[idx] = _num(disp)
            if len(pts) >= 2:
                xs = sorted(pts)
                slope = (pts[xs[-1]] - pts[xs[0]]) / (xs[-1] - xs[0])   # % avant par cran
                ref = idx if idx in pts else xs[0]
                if slope:
                    new = int(round(ref + (bf - pts[ref]) / slope))
                    if new != idx and new >= 0 and abs(new - idx) <= 15:
                        front = pts[ref] + (new - ref) * slope
                        to_disp = known.get(new, f"{front:.1f}:{100 - front:.1f}")
                        from_disp = known.get(idx, disp)
                        out.append({"section": "CONTROLS", "key": "RearBrakeSetting", "label": label("CONTROLS", "RearBrakeSetting"),
                                    "from": idx, "to": new, "from_disp": from_disp, "to_disp": to_disp, "live": True,
                                    "why": L(f"{why} ({bf:.1f} % à l'avant)".replace(".", ","), f"{why} ({bf:.1f} % front)", f"{why} (ön %{bf:.1f})")})
        for k, key in (("tc", "TractionControlMapSetting"), ("abs", "AntilockBrakeSystemMapSetting")):
            v, cur = live.get(k), s.get("CONTROLS", key)
            if not v or not cur:
                continue
            idx, disp, _ = cur
            if _num(disp) is None or int(_num(disp)) != idx or v == idx:
                continue   # réglage absent sur cette voiture (N/A) ou déjà à la bonne valeur
            out.append({"section": "CONTROLS", "key": key, "label": label("CONTROLS", key), "from": idx, "to": v,
                        "from_disp": disp, "to_disp": str(v), "live": True, "why": L(f"{why} (sur {v})", f"{why} ({v})", f"{why} ({v})")})
        return out

    def _fuel(self, s: Setup, fuel: dict) -> dict | None:
        """Essence de départ (fichier Course) : litres -> cran du réglage FuelSetting."""
        litres = (fuel or {}).get("litres")
        cur = s.get("GENERAL", "FuelSetting")
        if not litres or not cur:
            return None
        idx, disp, _ = cur
        known = self.catalog.get((s.vehicle, "GENERAL", "FuelSetting"), {})
        pts = {i: _num(d) for i, d in known.items() if _num(d) is not None}
        if _num(disp) is not None:
            pts[idx] = _num(disp)
        if len(pts) >= 2:
            xs = sorted(pts)
            slope = (pts[xs[-1]] - pts[xs[0]]) / (xs[-1] - xs[0]) or 1.0
        else:
            slope = 1.0                     # LMU : 1 cran = 1 litre
        ref = idx if idx in pts else min(pts) if pts else idx
        if ref not in pts:
            return None
        new = max(0, int(math.ceil(ref + (litres - pts[ref]) / slope - 1e-6)))
        if new == idx:
            return None
        val = pts[ref] + (new - ref) * slope
        return {"section": "GENERAL", "key": "FuelSetting", "label": L("Essence au départ", "Starting fuel", "Başlangıç yakıtı"), "from": idx, "to": new,
                "from_disp": disp.split("(")[0].strip() or disp, "to_disp": f"{val:.1f}L", "fuel": True,
                "why": L("essence pour la course : ", "fuel for the race: ", "yarış için yakıt: ") + fuel.get("why", "")}

    def propose(self, track: str, issues: list, findings: list, live: dict | None = None,
                fuel: dict | None = None) -> dict:
        self.scan()
        st = self.state(track)
        if self.factory:
            return {**st, "changes": [], "error": FACTORY_MSG()}
        if not st["folder"] or not st["current"]:
            return {**st, "changes": [], "error": L("Choisis d'abord le setup que tu utilises.", "First choose the setup you're using.", "Önce kullandığın setup'ı seç.")}
        path = self.dir / st["folder"] / st["current"]
        if not path.exists():
            return {**st, "changes": [], "error": L("Fichier introuvable.", "File not found.", "Dosya bulunamadı.")}
        s = Setup(path)
        changes, used, conflict = [], set(), {}
        for ch in self._live(s, live or {}):      # ce que tu as réglé toi-même en piste passe en premier
            changes.append(ch)
            used.add((ch["section"], ch["key"]))
        if self.mode == "Course":
            ch = self._fuel(s, fuel)
            if ch:
                changes.append(ch)
                used.add(("GENERAL", "FuelSetting"))

        def add(section, key, delta, why):
            if (section, key) in used:
                return
            old = conflict.get((section, key))
            if old is not None and old != delta:
                return
            ch = self._step(s, section, key, delta)
            if ch:
                ch["why"] = why
                changes.append(ch)
                used.add((section, key))
                conflict[(section, key)] = delta

        # répartition de freinage : on raisonne en "% à l'avant"
        bias_dir = self._direction(s, "CONTROLS", "RearBrakeSetting", -1)   # index↑ = moins d'avant (Oreca)

        def bias(towards_front: bool, why: str):
            add("CONTROLS", "RearBrakeSetting", bias_dir if towards_front else -bias_dir, why)

        for it in issues:
            k, w, why = it["kind"], it["where"], it["text"]
            if k == "blocage":
                bias(towards_front="arrière" in w, why=why)
            elif k == "patinage":
                add("CONTROLS", "TractionControlMapSetting", +1, why)
            elif k == "sous-virage":
                if w == "entrée":
                    bias(False, why)
                    add("SUSPENSION", "FrontAntiSwaySetting", -1, why)
                elif w == "milieu":
                    add("SUSPENSION", "FrontAntiSwaySetting", -1, why)
                    add("FRONTWING", "FWSetting", +1, why)
                else:
                    add("DRIVELINE", "DiffPowerSetting", -1, why)
                    add("SUSPENSION", "FrontAntiSwaySetting", -1, why)
            elif k == "survirage":
                if w == "entrée":
                    bias(True, why)
                    add("DRIVELINE", "DiffCoastSetting", +1, why)
                elif w == "milieu":
                    add("SUSPENSION", "RearAntiSwaySetting", -1, why)
                    add("REARWING", "RWSetting", +1, why)
                else:
                    add("CONTROLS", "TractionControlMapSetting", +1, why)
                    add("SUSPENSION", "RearAntiSwaySetting", -1, why)

        sym = (s.get("GENERAL", "Symmetric") or (0,))[0] == 1
        names = L("avant gauche|avant droit|arrière gauche|arrière droit", "front left|front right|rear left|rear right", "ön sol|ön sağ|arka sol|arka sağ").split("|")
        for kind, i in findings:
            wheels = [i] if i is not None else []
            if sym and i is not None:
                wheels = [i, i ^ 1]   # même réglage des deux côtés
            for wi in wheels:
                sec = WHEEL_SECTIONS[wi]
                why = L(f"températures du pneu {names[i]}", f"{names[i]} tyre temperatures", f"{names[i]} lastik sıcaklıkları") if i is not None else L("fond plat qui touche", "floor bottoming", "taban yere sürtüyor")
                if kind in ("camber_less", "camber_more"):
                    # valeur affichée négative : "moins de carrossage" = valeur qui monte vers 0
                    d = self._direction(s, sec, "CamberSetting", +1)
                    add(sec, "CamberSetting", d if kind == "camber_less" else -d, why)
                elif kind in ("pressure_down", "pressure_up"):
                    d = self._direction(s, sec, "PressureSetting", +1)
                    add(sec, "PressureSetting", -d if kind == "pressure_down" else d, why)
            if kind == "ride_up":
                for sec in WHEEL_SECTIONS:
                    d = self._direction(s, sec, "RideHeightSetting", +1)
                    add(sec, "RideHeightSetting", d, L("fond plat qui touche", "floor bottoming", "taban yere sürtüyor"))

        return {**st, "vehicle": s.vehicle, "changes": changes, "error": None}

    # ----------------------------------------------------------- création
    def create(self, track: str, changes: list[dict]) -> dict:
        """Met à jour LE fichier "APEX <circuit>.svm" (un seul par circuit).
        Chaque version est aussi copiée dans setup_backups/ pour pouvoir revenir en arrière."""
        st = self.state(track)
        if not st["folder"] or not st["current"] or not changes:
            return {"error": L("Rien à créer.", "Nothing to create.", "Oluşturulacak bir şey yok.")}
        folder = self.dir / st["folder"]
        base = Setup(folder / st["current"])
        name = self.apex_name(st["folder"], base)
        apex = folder / name
        if apex.exists() and not Setup(apex).is_apex():
            return {"error": L(f"Un fichier « {name} » existe déjà et n'a pas été créé par Apex Radio : je n'y touche pas.",
                               f"A file \"{name}\" already exists and wasn't created by Apex Radio: I won't touch it.",
                               f"\"{name}\" adlı bir dosya zaten var ve Apex Radio tarafından oluşturulmamış: ona dokunmuyorum.")}
        h = self.history.setdefault(self._hkey(st["folder"]), {"versions": [], "results": {}})
        bdir = BACKUPS / re.sub(r"[^A-Za-z0-9_-]+", "_", f"{st['folder']}_{self.mode}")
        bdir.mkdir(parents=True, exist_ok=True)
        # v1 = le setup de départ (le tien), gardé pour pouvoir y revenir
        if not h["versions"]:
            shutil.copyfile(base.path, bdir / "v1.svm")
            h["versions"].append({"version": 1, "label": "v1", "base": st["current"], "date": time.strftime("%d/%m/%Y %H:%M"),
                                  "changes": [], "backup": str(bdir / "v1.svm"), "origin": True})
            if st["label"] and st["label"] in h.get("results", {}):
                h["results"]["v1"] = h["results"].pop(st["label"])
        n = max(v["version"] for v in h["versions"]) + 1
        from_label = st["label"] if st["current"] == name else "v1" if st["current"] == h["versions"][0]["base"] else st["current"]
        note = "Apex Radio %s v%d (depuis %s) : %s" % (self.mode, n, from_label, "; ".join(
            f"{c['label']} {c['from_disp']} -> {c['to_disp']}" for c in changes))
        try:
            base.write_copy(apex, changes, note[:500], overwrite=apex.exists())
        except PermissionError:
            return {"error": L("Windows refuse l'écriture dans le dossier des setups. "
                               "Lance Apex Radio en administrateur, ou déplace LMU hors de Program Files.",
                               "Windows won't allow writing to the setups folder. "
                               "Run Apex Radio as administrator, or move LMU out of Program Files.",
                               "Windows setup klasörüne yazmaya izin vermiyor. " "Apex Radio'yu yönetici olarak çalıştır ya da LMU'yu Program Files dışına taşı.")}
        shutil.copyfile(apex, bdir / f"v{n}.svm")
        h["versions"].append({"version": n, "label": f"v{n}", "base": from_label, "date": time.strftime("%d/%m/%Y %H:%M"),
                              "backup": str(bdir / f"v{n}.svm"),
                              "changes": [{k: c[k] for k in ("label", "key", "section", "from_disp", "to_disp", "why")} for c in changes]})
        h["current"], h["apex_file"], h["apex_label"] = name, name, f"v{n}"
        self._save()
        self.scan(force=True)
        return {"file": name, "version": f"v{n}", "error": None}

    def restore(self, track: str, version: int) -> dict:
        """Remet une ancienne version dans le fichier APEX du circuit."""
        st = self.state(track)
        h = self.history.get(self._hkey(st["folder"]), {})
        v = next((x for x in h.get("versions", []) if x["version"] == version), None)
        if not v or not Path(v["backup"]).exists():
            return {"error": L("Version introuvable.", "Version not found.", "Sürüm bulunamadı.")}
        name = self.apex_name(st["folder"], Setup(Path(v["backup"])))
        apex = self.dir / st["folder"] / name
        if apex.exists() and not Setup(apex).is_apex():
            return {"error": L(f"« {name} » n'a pas été créé par Apex Radio : je n'y touche pas.",
                               f"\"{name}\" wasn't created by Apex Radio: I won't touch it.",
                               f"\"{name}\" Apex Radio tarafından oluşturulmamış: ona dokunmuyorum.")}
        src = Setup(Path(v["backup"]))
        try:
            src.write_copy(apex, [], f"Apex Radio {self.mode} v{version} (version restaurée)", overwrite=apex.exists())
        except PermissionError:
            return {"error": L("Windows refuse l'écriture dans le dossier des setups.", "Windows won't allow writing to the setups folder.", "Windows setup klasörüne yazmaya izin vermiyor.")}
        h["current"], h["apex_file"], h["apex_label"] = name, name, f"v{version}"
        self._save()
        return {"file": name, "version": f"v{version}", "error": None}
