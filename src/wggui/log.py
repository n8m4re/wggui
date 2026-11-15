"""App log: file and window display for diagnosing failures.

Location: $WGGUI_LOG, otherwise $XDG_STATE_HOME/wireguard-gui/wggui.log (XDG Base Directory Specification:
“state” holds history/logs; default ~/.local/state). Rotate at 1 MB, retaining 3 old files
(logging.handlers.RotatingFileHandler, Python docs).
Level: $WGGUI_LOG_LEVEL (e.g. DEBUG includes full commands); default INFO.

Security (keys never leave the server, see CONTRIBUTING.md): every line passes through `mask()`.
Replace anything resembling a WireGuard key (Base64 of 32 bytes), including public keys, because text
alone cannot identify which keys are public. Never log SSH stdout or stdin.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

LOGGER = "wggui"
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MASK = "‹Schlüssel›"
# 43 Base64 characters plus “=”; the last character encodes only 4 bits → only these 16 are possible
KEY_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=(?![A-Za-z0-9+/=])")


def mask(text: str) -> str:
    return KEY_RE.sub(MASK, text)


class MaskingFormatter(logging.Formatter):
    """Mask the complete line, including arguments and tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record))


def default_log_path() -> Path:
    if env := os.environ.get("WGGUI_LOG"):
        return Path(env)
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "wireguard-gui" / "wggui.log"


def setup_logging(path: Path | None = None, level: str | None = None) -> Path:
    """Configure file and stderr output for the “wggui” logger; return the log path."""
    path = path or default_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER)
    logger.setLevel((level or os.environ.get("WGGUI_LOG_LEVEL") or "INFO").upper())
    for handler in list(logger.handlers):  # Replace handlers on repeated calls; avoid duplicates
        logger.removeHandler(handler)
        handler.close()
    file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    stderr_handler = logging.StreamHandler(sys.stderr)
    for handler in (file_handler, stderr_handler):
        handler.setFormatter(MaskingFormatter(FORMAT))
        logger.addHandler(handler)
    logger.propagate = False
    os.chmod(path, 0o600)  # Owner only: contains hostnames and commands
    return path


def install_excepthook() -> None:
    """Log uncaught errors (e.g. in Qt slots) with tracebacks, not just to stderr."""
    log = logging.getLogger(LOGGER)

    def hook(exc_type, exc, tb):  # noqa: ANN001 – sys.excepthook signature
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Unbehandelter Fehler", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook
