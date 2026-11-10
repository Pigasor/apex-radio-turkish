"""
Langue de l'appli (français / anglais / turc).

L("texte français", "English text", "Türkçe metin") renvoie la bonne version selon la langue choisie.
Le turc est facultatif : sans troisième texte, c'est l'anglais qui sert (traduction au fur et à mesure).
dec("3.6") : virgule décimale en français et en turc, point en anglais.
"""

from __future__ import annotations

LANGS = ("fr", "en", "tr")
LANG = "fr"


def set_lang(lang: str) -> None:
    global LANG
    code = str(lang).lower()[:2]
    LANG = code if code in LANGS else "fr"


def en() -> bool:
    return LANG == "en"


def fr() -> bool:
    return LANG == "fr"


def tr() -> bool:
    return LANG == "tr"


def L(fr: str, en_text: str, tr_text: str | None = None) -> str:
    if LANG == "tr":
        return en_text if tr_text is None else tr_text
    return en_text if LANG == "en" else fr


def dec(s: str) -> str:
    return s if LANG == "en" else s.replace(".", ",")


def plural(n: float, fr_word: str, en_word: str | None = None, tr_word: str | None = None) -> str:
    """'1 tour' / '3 tours', '1 lap' / '3 laps', '1 tur' / '3 tur' (pas de pluriel après un nombre en turc)."""
    if LANG == "tr" and tr_word:
        return tr_word
    english = LANG in ("en", "tr") and bool(en_word)
    w = en_word if english else fr_word
    return w + ("s" if abs(n) >= 2 or (english and abs(n) != 1) else "")


def session(code: str) -> str:
    """Nom de session affiché ("course" -> "race"...)."""
    if LANG == "tr":
        return {"course": "yarış", "qualif": "sıralama", "essais": "antrenman", "warmup": "ısınma"}.get(code, code)
    if LANG != "en":
        return code
    return {"course": "race", "qualif": "qualifying", "essais": "practice", "warmup": "warm-up"}.get(code, code)
