"""Entry point: `uv run wggui`."""

from __future__ import annotations

import logging
import os
import platform
import sys

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator, qVersion
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from wggui import __version__, i18n, settings
from wggui.db import Database, default_db_path
from wggui.log import install_excepthook, setup_logging
from wggui.ui.main_window import MainWindow


def language_setting() -> str:
    """Get the selected language from settings (“system” = system language)."""
    return settings.get("language", "system")


def save_language_setting(code: str) -> None:
    settings.set_value("language", code)


def install_qt_translation(app: QApplication, locale: QLocale | None = None) -> bool:
    """Translate Qt labels (Cancel, Close, Yes/No) into the app language; Qt docs “Internationalization
    with Qt”: QTranslator.load(locale, "qtbase", "_", QLibraryInfo TranslationsPath)."""
    locale = locale or QLocale()
    if locale.language() == QLocale.Language.English:
        return True  # Qt labels are English; otherwise load() would fall back to the system language
    translator = QTranslator(app)
    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(f"qtbase_{locale.name().split('_')[0]}", path):
        app.installTranslator(translator)
        return True
    return False


def prepare_language() -> str:
    """Set the language before Qt starts: app catalogs, default locale, and LANGUAGE. The KDE platform theme
    translates standard buttons (Cancel, Close, etc.) from LANGUAGE, not the app (gettext convention)."""
    language = i18n.set_language(i18n.resolve(language_setting(), QLocale.system().name()))
    QLocale.setDefault(QLocale(language))
    os.environ["LANGUAGE"] = language
    return language


def main() -> int:
    language = prepare_language()
    app = QApplication(sys.argv)
    app.setApplicationName("wireguard-gui")
    app.setApplicationDisplayName("WGGUI")
    # Match the .desktop entry (Wayland window icon); in Flatpak it uses the app ID (FLATPAK_ID)
    app.setDesktopFileName(os.environ.get("FLATPAK_ID", "wireguard-gui"))
    # Window icon for X11 and the task switcher: the app icon from the icon theme (scripts/install-desktop.sh or
    # Flatpak install it under the desktop file name), otherwise the generic VPN icon of the theme
    app.setWindowIcon(QIcon.fromTheme(app.desktopFileName(), QIcon.fromTheme("network-vpn")))
    log_path = setup_logging()
    install_excepthook()
    log = logging.getLogger("wggui")
    log.info("Start wggui %s – Python %s, PySide6 %s, Qt %s, %s", __version__, platform.python_version(),
             pyside_version, qVersion(), app.platformName())
    db_path = default_db_path()
    log.info("Datenbank: %s, Log: %s", db_path, log_path)
    log.info("Sprache: %s (Einstellung: %s, System: %s)", language, language_setting(), QLocale.system().name())
    if not install_qt_translation(app, QLocale(language)):
        log.info("Keine Qt-Übersetzung für %s gefunden – Standardknöpfe bleiben englisch", QLocale().name())
    db = Database(db_path)
    window = MainWindow(db, log_path=log_path)
    window.show()
    code = app.exec()
    db.close()
    log.info("Ende (Code %d)", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
