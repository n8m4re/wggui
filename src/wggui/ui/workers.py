"""Run SSH calls in the background to keep the window responsive (QThreadPool/QRunnable, Qt docs).

Signals belong to a QObject created in the GUI thread, so slots run there (queued connection).
The database is accessed only in that thread.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from wggui.remote import CommandError

log = logging.getLogger(__name__)


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as err:  # noqa: BLE001 – Show errors in the GUI thread
            if not isinstance(err, CommandError):  # Command errors are already logged
                log.exception("Fehler in Hintergrundaufgabe")
            self.signals.failed.emit(str(err))
            return
        self.signals.done.emit(result)


class Background:
    """Keep running tasks alive until they finish."""

    def __init__(self, pool: QThreadPool | None = None):
        self.pool = pool or QThreadPool.globalInstance()
        self._tasks: set[_Task] = set()

    @property
    def busy(self) -> bool:
        return bool(self._tasks)

    def start(self, fn: Callable[[], Any], on_done: Callable[[Any], None], on_error: Callable[[str], None]) -> None:
        task = _Task(fn)
        task.setAutoDelete(False)
        self._tasks.add(task)

        def finish_ok(result: Any) -> None:
            self._tasks.discard(task)
            on_done(result)

        def finish_err(message: str) -> None:
            self._tasks.discard(task)
            on_error(message)

        task.signals.done.connect(finish_ok)
        task.signals.failed.connect(finish_err)
        self.pool.start(task)

    def wait(self, msecs: int = 30000) -> bool:
        return self.pool.waitForDone(msecs)
