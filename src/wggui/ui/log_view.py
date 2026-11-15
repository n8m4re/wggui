"""Dockable log window: show this session's log entries and the log and database paths.

Worker-thread log entries reach the GUI thread through a Qt signal (queued connection; Qt docs:
"Signals & Slots" / "Threads and QObjects"). Mask data as in the log file (log.MaskingFormatter).
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from wggui.i18n import _
from wggui.log import LOGGER, MaskingFormatter

MAX_LINES = 5000
COLORS = {logging.WARNING: "#c87f0a", logging.ERROR: "#d0453a", logging.CRITICAL: "#d0453a"}


class _Emitter(QObject):
    line = Signal(str, int)


class QtLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.emitter = _Emitter()
        self.setFormatter(MaskingFormatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if not isValid(self.emitter):  # Window already closed
            return
        try:
            self.emitter.line.emit(self.format(record), record.levelno)
        except Exception:  # noqa: BLE001 – Logging must never crash the app
            self.handleError(record)


class LogDock(QDockWidget):
    def __init__(self, parent: QWidget | None, log_path: Path | None, db_path: Path | None):
        super().__init__(_("Protokoll"), parent)
        self.setObjectName("protokoll")
        self.log_path = log_path
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(MAX_LINES)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        where = [f"Log-Datei: {log_path}" if log_path else _("Log-Datei: keine (nur Anzeige)")]
        if db_path:
            where.append(_("Datenbank: {db_path}").format(db_path=db_path))
        self.paths = QLabel("  ·  ".join(where))
        self.paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.paths.setWordWrap(True)

        open_file = QPushButton(_("Log-Datei öffnen"))
        open_file.clicked.connect(lambda: self._open(self.log_path))
        open_dir = QPushButton(_("Ordner öffnen"))
        open_dir.clicked.connect(lambda: self._open(self.log_path.parent if self.log_path else None))
        for button in (open_file, open_dir):
            button.setEnabled(log_path is not None)
        clear = QPushButton(_("Anzeige leeren"))
        clear.clicked.connect(self.text.clear)

        buttons = QHBoxLayout()
        buttons.addWidget(self.paths, 1)
        for button in (open_file, open_dir, clear):
            buttons.addWidget(button)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(buttons)
        layout.addWidget(self.text)
        self.setWidget(body)

        self.handler = QtLogHandler()
        self.handler.emitter.line.connect(self.append)
        logger = logging.getLogger(LOGGER)
        if logger.level == logging.NOTSET:  # Show INFO even without setup_logging (tests)
            logger.setLevel(logging.INFO)
        logger.addHandler(self.handler)

    def append(self, line: str, level: int) -> None:
        color = COLORS.get(level)
        text = html.escape(line)
        self.text.appendHtml(f'<span style="color:{color}">{text}</span>' if color else text)
        self.text.horizontalScrollBar().setValue(0)  # Keep the line start (time, level) visible

    def lines(self) -> list[str]:
        return self.text.toPlainText().splitlines()

    def detach(self) -> None:
        logging.getLogger(LOGGER).removeHandler(self.handler)

    @staticmethod
    def _open(path: Path | None) -> None:
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
