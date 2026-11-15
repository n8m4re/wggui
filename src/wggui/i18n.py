"""Localization: German source text; English, Spanish, Italian, Chinese, and Japanese translations.

- German code text uses `_()` (Python `gettext` convention), plurals use `ngettext()`.
  `N_()` only marks import-time constants; `_()` translates them for display.
- Catalogs: `wggui/locale/<sprache>.po` (GNU gettext PO format). The app reads them directly,
  including in Flatpak; no `msgfmt` needed. Maintain with `python3 scripts/i18n.py extract` / `check` (docs/i18n.md).
- Language: setting (View → Language), then `WGGUI_LANG`, then system language; unsupported → English.
- Logs remain in German (developer tool).
Sources: Python `gettext` docs (GNUTranslations, ngettext), GNU gettext Manual (PO format, Plural-Forms).
"""

from __future__ import annotations

import ast
import gettext
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SOURCE = "de"
LANGUAGES = {"de": "Deutsch", "en": "English", "es": "Español", "it": "Italiano", "zh": "中文", "ja": "日本語"}
FALLBACK = "en"
LOCALE_DIR = Path(__file__).parent / "locale"
# Per GNU gettext Manual: de/en/es/it have two plural forms (n != 1); zh/ja have one
_ONE_FORM = {"zh", "ja"}

_current = SOURCE
_catalog: dict[str, str] = {}
_plurals: dict[str, list[str]] = {}


def _unquote(text: str) -> str:
    return ast.literal_eval(text)  # PO strings use C-style "…" literals (\n, \t, \", \\)


def parse_po(text: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Read basic PO entries: msgid, msgid_plural, msgstr, msgstr[n]; skip comments and empty translations."""
    singles: dict[str, str] = {}
    plurals: dict[str, list[str]] = {}
    entry: dict[str, str] = {}
    key = ""

    def flush() -> None:
        msgid = entry.get("msgid")
        if msgid:  # Empty msgid is the header
            if "msgid_plural" in entry:
                forms = [entry[k] for k in sorted(k for k in entry if k.startswith("msgstr["))]
                if any(forms):
                    plurals[msgid] = forms
            elif entry.get("msgstr"):
                singles[msgid] = entry["msgstr"]
        entry.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            if not line and entry:
                flush()
            continue
        if line.startswith('"'):
            entry[key] = entry.get(key, "") + _unquote(line)
            continue
        word, _sep, rest = line.partition(" ")
        if word == "msgid" and any(k.startswith("msgstr") for k in entry):
            flush()
        key = word
        entry[key] = _unquote(rest)
    flush()
    return singles, plurals


def available() -> list[str]:
    return [SOURCE] + sorted(p.stem for p in LOCALE_DIR.glob("*.po") if p.stem in LANGUAGES)


def _code(locale_name: str) -> str:
    return locale_name.replace("-", "_").split("_")[0].split(".")[0].lower()


def resolve(choice: str | None, system: str | None = None) -> str:
    """Use setting, then WGGUI_LANG, then system language; unknown system languages → English.
    With none available (e.g. tests), use German, the source language."""
    for candidate in (choice, os.environ.get("WGGUI_LANG")):
        if candidate and candidate != "system" and _code(candidate) in LANGUAGES:
            return _code(candidate)
    if system:
        return _code(system) if _code(system) in LANGUAGES else FALLBACK
    return SOURCE


def set_language(code: str) -> str:
    """Load the catalog and return the language actually in use."""
    global _current, _catalog, _plurals
    code = code if code in LANGUAGES else FALLBACK
    _catalog, _plurals, _current = {}, {}, SOURCE
    if code == SOURCE:
        return SOURCE
    po = LOCALE_DIR / f"{code}.po"
    try:
        _catalog, _plurals = parse_po(po.read_text(encoding="utf-8"))
    except OSError as err:
        log.warning("Sprachkatalog %s nicht lesbar: %s – bleibe bei Deutsch", po, err)
        return SOURCE
    _current = code
    return code


def current() -> str:
    return _current


def _(text: str) -> str:  # noqa: N802 – gettext convention
    return _catalog.get(text, text)


def N_(text: str) -> str:  # noqa: N802 – marker for extraction only
    return text


def ngettext(one: str, many: str, n: int) -> str:
    forms = _plurals.get(one)
    if forms:
        index = 0 if _current in _ONE_FORM else (0 if n == 1 else 1)
        if index < len(forms) and forms[index]:
            return forms[index]
    return one if n == 1 else many


# Gettext-compatible object for tests and tools
def translations() -> gettext.NullTranslations:
    class _T(gettext.NullTranslations):
        def gettext(self, message: str) -> str:
            return _(message)

        def ngettext(self, msgid1: str, msgid2: str, n: int) -> str:
            return ngettext(msgid1, msgid2, n)

    return _T()
