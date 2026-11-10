"""
Côté bureau Windows : fenêtre Apex Radio et icône près de l'horloge.

- Fenêtre : intégrée à Apex Radio (pywebview + WebView2, le moteur d'Edge inclus dans Windows),
  avec le logo Apex dans la barre de titre et la barre des tâches.
  Secours : Edge (ou Chrome) en mode application, puis le navigateur par défaut.
- Icône près de l'horloge (pystray) : ouvrir le tableau de bord, radio, silence, quitter.
  Fermer la fenêtre n'arrête pas Apex Radio : la radio continue, on la rouvre par l'icône.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from i18n import L

HERE = Path(__file__).resolve().parent
from paths import DATA, LOCAL  # noqa: E402


def _browser_app() -> str | None:
    """Chemin d'Edge (installé sur tous les Windows 10/11), sinon de Chrome."""
    if sys.platform != "win32":
        return None
    cands = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "Microsoft/Edge/Application/msedge.exe")
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "Google/Chrome/Application/chrome.exe")
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("msedge") or shutil.which("chrome")


def open_window(url: str) -> None:
    """Ouvre (ou ramène au premier plan) le tableau de bord."""
    if WINDOW is not None and WINDOW.ready and url.rstrip("/") == WINDOW.url.rstrip("/"):
        WINDOW.show()
        return
    if url.endswith("/tel"):          # vue téléphone : dans le navigateur, c'est fait pour être copiée
        webbrowser.open(url)
        return
    open_app_window(url)


def open_app_window(url: str) -> None:
    """Secours : Edge / Chrome en mode application."""
    exe = _browser_app()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1500,920", "--no-first-run",
                              "--no-default-browser-check"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
            return
        except OSError as e:
            print(f"  Fenêtre Apex impossible ({e}), j'ouvre le navigateur.")
    webbrowser.open(url)


def app_id() -> None:
    """Regroupe la fenêtre sous « Apex Radio » dans la barre des tâches (et pas sous Python)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ApexRadio.App")
        except Exception:
            pass


def _set_icon(hwnd: int) -> None:
    """Logo Apex dans la barre de titre et la barre des tâches."""
    import ctypes
    from ctypes import c_int, c_uint, c_void_p, c_wchar_p
    u = ctypes.windll.user32
    u.LoadImageW.restype = c_void_p
    u.LoadImageW.argtypes = [c_void_p, c_wchar_p, c_uint, c_int, c_int, c_uint]
    u.SendMessageW.argtypes = [c_void_p, c_uint, c_void_p, c_void_p]
    ico = str(HERE / "apex.ico")
    for kind, metric in ((1, 11), (0, 49)):          # ICON_BIG / SM_CXICON, ICON_SMALL / SM_CXSMICON
        size = u.GetSystemMetrics(metric)
        h = u.LoadImageW(None, ico, 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
        if h:
            u.SendMessageW(c_void_p(hwnd), 0x80, c_void_p(kind), c_void_p(h))   # WM_SETICON


class AppWindow:
    """Fenêtre Apex Radio intégrée (pywebview)."""

    def __init__(self, url: str, hide_on_close: bool, on_hidden=None) -> None:
        self.url, self.hide_on_close, self.on_hidden = url, hide_on_close, on_hidden
        self.win = None
        self.ready = False
        self.quitting = False

    @staticmethod
    def available() -> bool:
        if sys.platform != "win32":
            return False
        try:
            import webview  # noqa: F401
            return True
        except Exception as e:
            print(f"  Fenêtre intégrée indisponible ({e.__class__.__name__}: {e})")
            return False

    def _hwnd(self) -> int | None:
        try:
            return int(self.win.native.Handle.ToInt64())
        except Exception:
            try:
                import ctypes
                return ctypes.windll.user32.FindWindowW(None, "Apex Radio") or None
            except Exception:
                return None

    def _shown(self) -> None:
        h = self._hwnd()
        if h:
            try:
                _set_icon(h)
            except Exception as e:
                print(f"  Icône de fenêtre : {e}")
        self.ready = True

    def _closing(self):
        if self.quitting or not self.hide_on_close:
            print("  Fenêtre fermée : arrêt d'Apex Radio.")
            threading.Timer(0.2, lambda: os._exit(0)).start()
            return True
        threading.Thread(target=self.win.hide, daemon=True).start()
        if self.on_hidden:
            self.on_hidden()
        return False      # on garde la radio : la fenêtre est seulement cachée

    def show(self) -> None:
        try:
            self.win.show()
            self.win.restore()
            h = self._hwnd()
            if h:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(h)
        except Exception as e:
            print(f"  Réouverture de la fenêtre : {e}")
            open_app_window(self.url)

    def run(self) -> None:
        """Bloque sur le fil principal jusqu'à la fin d'Apex Radio."""
        import webview
        global WINDOW
        self.win = webview.create_window("Apex Radio", self.url, width=1500, height=920, min_size=(1000, 640),
                                         background_color="#06070B", text_select=True)
        self.win.events.shown += self._shown
        self.win.events.closing += self._closing
        WINDOW = self
        store = LOCAL / "webview"
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(store))
        # fin de la boucle : la fenêtre a été détruite
        os._exit(0)


WINDOW: AppWindow | None = None


def _post(url: str) -> None:
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=3).close()
    except Exception as e:
        print(f"  Action de l'icône impossible ({e.__class__.__name__})")


class Tray:
    """Icône Apex Radio près de l'horloge."""

    def __init__(self, state, base_url: str, phone_url: str, cfg: dict | None = None, save=None) -> None:
        self.state, self.base, self.phone = state, base_url, phone_url
        self.cfg, self.save = cfg, save
        self.icon = None

    def available(self) -> bool:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
            return True
        except Exception as e:
            print(f"  Icône près de l'horloge indisponible ({e.__class__.__name__}: {e})")
            return False

    def _menu(self):
        import pystray
        v = self.state.voice
        item, sep = pystray.MenuItem, pystray.Menu.SEPARATOR
        return pystray.Menu(
            item(lambda _: L("Ouvrir le tableau de bord", "Open the dashboard", "Gösterge panelini aç"), lambda: open_window(self.base), default=True),
            item(lambda _: L("Vue téléphone : ", "Phone view: ", "Telefon görünümü: ") + self.phone.replace("http://", ""), lambda: open_window(self.phone)),
            sep,
            item(lambda _: "Radio", lambda: _post(self.base + "/api/voice/toggle"), checked=lambda _: bool(v.enabled)),
            item(lambda _: L("Silence (urgent seulement)", "Quiet (urgent only)", "Sessiz (yalnızca acil)"), lambda: _post(self.base + "/api/voice/quiet"),
                 checked=lambda _: bool(v.quiet)),
            sep,
            item(lambda _: L("Ouvrir le journal (dépannage)", "Open the log (troubleshooting)", "Günlüğü aç (sorun giderme)"),
                 lambda: os.startfile(LOCAL / "journal.txt") if hasattr(os, "startfile") else None),
            item(lambda _: L("Quitter Apex Radio", "Quit Apex Radio", "Apex Radio'dan çık"), self.quit),
        )

    def quit(self) -> None:
        print("  Arrêt demandé depuis l'icône.")
        try:
            if self.icon:
                self.icon.stop()
        finally:
            threading.Timer(0.3, lambda: os._exit(0)).start()

    def hint(self) -> None:
        """Première fermeture de la fenêtre : on explique où est passée la radio."""
        if self.icon is None or self.cfg is None or self.cfg.get("tray_hint"):
            return
        try:
            self.icon.notify(L("Apex Radio continue ici. Clique sur cette icône pour rouvrir le tableau de bord, "
                               "clic droit pour quitter.",
                               "Apex Radio keeps running here. Click this icon to reopen the dashboard, "
                               "right-click to quit.",
                               "Apex Radio burada çalışmaya devam ediyor. Gösterge panelini yeniden açmak için bu simgeye tıkla, " "çıkmak için sağ tıkla."), "Apex Radio")
            self.cfg["tray_hint"] = True
            if self.save:
                self.save(self.cfg)
        except Exception:
            pass

    def run(self, detached: bool = False) -> None:
        """Bloque (fil principal), ou tourne à côté de la fenêtre si detached."""
        import pystray
        from PIL import Image
        img = Image.open(HERE / "apex.ico")
        self.icon = pystray.Icon("ApexRadio", img, "Apex Radio", self._menu())

        def setup(icon) -> None:
            icon.visible = True

        def keep_fresh() -> None:   # coches Radio / Silence et langue à jour, même changées depuis le tableau de bord
            import time
            while True:
                time.sleep(3)
                self.refresh()
        threading.Thread(target=keep_fresh, daemon=True).start()
        if detached:
            self.icon.run_detached(setup=setup)
        else:
            self.icon.run(setup=setup)

    def refresh(self) -> None:
        """Après un changement de langue."""
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass


def show_existing(base_url: str) -> None:
    """Apex Radio tourne déjà : on lui demande de rouvrir sa fenêtre."""
    try:
        urllib.request.urlopen(urllib.request.Request(base_url + "/api/show", data=b"{}", method="POST"), timeout=3).close()
    except Exception:
        open_app_window(base_url)

