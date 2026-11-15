"""UI settings (QSettings: ~/.config/wireguard-gui/wireguard-gui.conf; app directory in Flatpak).

Only UI preferences (language, auto refresh); hosts and peers live in the database.
Tests set WGGUI_SETTINGS to a file to avoid changing the real configuration.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QSettings

ORG = APP = "wireguard-gui"


def _settings() -> QSettings:
    if path := os.environ.get("WGGUI_SETTINGS"):
        return QSettings(path, QSettings.Format.IniFormat)
    return QSettings(ORG, APP)


def get(key: str, default: str = "") -> str:
    return str(_settings().value(key, default))


def get_bool(key: str, default: bool = False) -> bool:
    return get(key, "true" if default else "false").lower() in ("1", "true", "yes")


def set_value(key: str, value: str | bool) -> None:
    s = _settings()
    s.setValue(key, ("true" if value else "false") if isinstance(value, bool) else value)
    s.sync()
