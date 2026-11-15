"""Test complete catalogs, language selection, plurals, and translated UI."""

import importlib.util
from pathlib import Path

import pytest

from wggui import i18n

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lang():
    """Set language for one test, then restore German source strings for other tests."""
    yield i18n.set_language
    i18n.set_language("de")


def _tool():
    spec = importlib.util.spec_from_file_location("i18n_tool", ROOT / "scripts" / "i18n.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalogs_complete_and_placeholders_match(capsys):
    assert _tool().cmd_check() == 0, capsys.readouterr().out


def test_resolve_language():
    assert i18n.resolve(None, "de_DE.UTF-8") == "de"
    assert i18n.resolve(None, "ja_JP") == "ja"
    assert i18n.resolve(None, "zh_CN.UTF-8") == "zh"
    assert i18n.resolve(None, "fr_FR") == "en"  # unsupported locale falls back to English
    assert i18n.resolve("it", "de_DE") == "it"  # setting takes precedence over system locale
    assert i18n.resolve("system", "es_ES") == "es"
    assert i18n.resolve(None) == "de"  # no preference: source strings


@pytest.mark.parametrize("code", ["en", "es", "it", "zh", "ja"])
def test_every_language_translates(lang, code):
    assert lang(code) == code
    assert i18n._("Host hinzufügen…") != "Host hinzufügen…"
    text = i18n._("Läuft · {active} von {total} Peers aktiv").format(active=1, total=5)
    assert "1" in text and "5" in text


def test_plural(lang):
    lang("en")
    assert i18n.ngettext("{n} Änderung", "{n} Änderungen", 1).format(n=1) == "1 change"
    assert i18n.ngettext("{n} Änderung", "{n} Änderungen", 3).format(n=3) == "3 changes"
    lang("ja")
    assert "3" in i18n.ngettext("{n} Änderung", "{n} Änderungen", 3).format(n=3)
    lang("de")
    assert i18n.ngettext("{n} Änderung", "{n} Änderungen", 3).format(n=3) == "3 Änderungen"


def test_unknown_text_falls_back_to_source(lang):
    lang("en")
    assert i18n._("Gibt es nicht im Katalog") == "Gibt es nicht im Katalog"


def test_main_window_in_english(qtbot, lang, tmp_path):
    from PySide6.QtWidgets import QToolBar

    from wggui.db import Database
    from wggui.ui.main_window import MainWindow

    lang("en")
    db = Database(tmp_path / "x.db")
    w = MainWindow(db)
    qtbot.addWidget(w)
    texts = [a.iconText() for a in w.findChildren(QToolBar)[0].actions() if not a.isSeparator()]
    assert "Add host…".rstrip("…") in [t.rstrip("…") for t in texts]
    assert "No host yet" in w.empty_text.text()
    headers = [w.table.horizontalHeaderItem(i).text() for i in range(w.table.columnCount())]
    assert "Last handshake" in headers
    db.close()
