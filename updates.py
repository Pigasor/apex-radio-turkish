"""
Vérification des mises à jour.

Au démarrage (puis toutes les 6 h), on demande à GitHub la dernière version publiée.
Si elle est plus récente que celle installée, le tableau de bord affiche un bandeau
avec un lien vers la page de téléchargement. Rien n'est téléchargé ni installé tout seul.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

REPO = "Pigasor/apex-radio-turkish"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE = f"https://github.com/{REPO}/releases/latest"
LIST = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
ALL = f"https://github.com/{REPO}/releases"
HERE = Path(__file__).resolve().parent


def _num(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def current() -> str:
    try:
        return (HERE / "VERSION").read_text(encoding="utf-8").strip() or "0"
    except OSError:
        return "0"


class Updates:
    def __init__(self) -> None:
        self.version = current()
        self.latest: str | None = None
        self.url = PAGE
        self.previous: dict | None = None   # version stable juste avant celle installée (pour revenir en arrière)

    def check(self) -> None:
        try:
            req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": "ApexRadio/" + self.version})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.load(r)
            tag = str(d.get("tag_name") or "").lstrip("vV")
            if tag and not d.get("draft") and not d.get("prerelease"):
                self.latest = tag
                self.url = d.get("html_url") or PAGE
                if self.available():
                    print(f"  Mise à jour disponible : {tag} (installée : {self.version})")
        except Exception as e:  # hors ligne, GitHub indisponible… pas grave
            print(f"  Vérification des mises à jour impossible ({e.__class__.__name__})")

    def check_previous(self) -> None:
        """Cherche la dernière version stable plus ancienne que celle installée (secours si une mise à jour pose problème)."""
        try:
            req = urllib.request.Request(LIST, headers={"Accept": "application/vnd.github+json",
                                                        "User-Agent": "ApexRadio/" + self.version})
            with urllib.request.urlopen(req, timeout=8) as r:
                rels = json.load(r)
            best = None
            for d in rels:
                tag = str(d.get("tag_name") or "").lstrip("vV")
                if not tag or d.get("draft") or d.get("prerelease") or "test" in tag:
                    continue
                if _num(tag) < _num(self.version) and (best is None or _num(tag) > _num(best[0])):
                    best = (tag, d.get("html_url") or ALL)
            self.previous = {"version": best[0], "url": best[1]} if best else None
        except Exception:
            pass

    def available(self) -> bool:
        return bool(self.latest) and _num(self.latest) > _num(self.version)

    def start(self) -> None:
        def run() -> None:
            while True:
                self.check()
                self.check_previous()
                time.sleep(6 * 3600)
        threading.Thread(target=run, daemon=True).start()

    def ui(self) -> dict:
        return {"version": self.version, "latest": self.latest if self.available() else None, "url": self.url,
                "previous": self.previous, "all": ALL}
