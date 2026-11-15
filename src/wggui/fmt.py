"""Display formats without Qt, for easier testing."""

from __future__ import annotations

import time

from wggui.i18n import _

# WireGuard renews the handshake within 2 minutes while traffic flows (wg(8)/whitepaper:
# REKEY_AFTER_TIME = 120 s). Older than 3 minutes → likely no active connection.
ACTIVE_SECONDS = 180


def bytes_h(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover


def handshake_h(timestamp: int, now: float | None = None) -> str:
    if timestamp <= 0:
        return _("nie")
    age = int((now if now is not None else time.time()) - timestamp)
    if age < 60:
        return _("vor {v1} s").format(v1=max(age, 0))
    if age < 3600:
        return _("vor {v1} min").format(v1=age // 60)
    if age < 86400:
        return _("vor {v1} h").format(v1=age // 3600)
    return _("vor {v1} d").format(v1=age // 86400)


def is_active(timestamp: int, now: float | None = None) -> bool:
    return timestamp > 0 and (now if now is not None else time.time()) - timestamp < ACTIVE_SECONDS


def plural(n: int, one: str, many: str) -> str:
    """Use proper singular and plural forms instead of a parenthesized suffix."""
    return f"{n} {one if n == 1 else many}"


def short_key(key: str | None) -> str:
    return f"{key[:8]}…" if key else "–"
