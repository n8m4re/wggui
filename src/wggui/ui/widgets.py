"""Small KDE HIG components not provided by Qt Widgets.

`MessageBar` replaces Kirigami.InlineMessage / KMessageWidget (KDE HIG "Communicating status changes":
show messages in the window with an icon and text, plus a subtle tinted background).
Use Breeze colors (Positive/Neutral/Negative/Active Text) for light and dark themes; blend backgrounds
with the current window color to follow the color scheme.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QStyle, QWidget

from wggui.i18n import _

ICONS = {"error": ("dialog-error", QStyle.StandardPixmap.SP_MessageBoxCritical),
         "warning": ("dialog-warning", QStyle.StandardPixmap.SP_MessageBoxWarning),
         "info": ("dialog-information", QStyle.StandardPixmap.SP_MessageBoxInformation)}

# Breeze color scheme: Negative, Neutral, Active, and Positive Text
TONES = {"error": "#da4453", "warning": "#f67400", "info": "#3daee9", "positive": "#27ae60"}


def theme_icon(widget: QWidget, kind: str) -> QIcon:
    name, fallback = ICONS[kind]
    return QIcon.fromTheme(name, widget.style().standardIcon(fallback))


def is_dark(widget: QWidget) -> bool:
    return widget.palette().color(QPalette.ColorRole.Window).lightnessF() < 0.5


def tone(widget: QWidget, kind: str) -> QColor:
    """Return state text color, slightly brighter in dark themes for contrast."""
    color = QColor(TONES[kind])
    return color.lighter(125) if is_dark(widget) else color


def mix(a: QColor, b: QColor, amount: float) -> QColor:
    return QColor.fromRgbF(a.redF() * (1 - amount) + b.redF() * amount,
                           a.greenF() * (1 - amount) + b.greenF() * amount,
                           a.blueF() * (1 - amount) + b.blueF() * amount)


def muted(widget: QWidget) -> str:
    """Get label color from the palette for light and dark themes."""
    return widget.palette().color(QPalette.ColorRole.PlaceholderText).name()


class MessageBar(QFrame):
    """Show a message with an icon, text, and optional button; hide when empty."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("messageBar")
        self.kind = ""
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.button = QPushButton()
        self.button.hide()
        self._slot: Callable[[], None] | None = None
        self.button.clicked.connect(lambda: self._slot and self._slot())
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 6, 6)
        layout.addWidget(self.icon)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hide()

    def show_message(self, kind: str, text: str, action: tuple[str, Callable[[], None]] | None = None,
                     detail: str = "") -> None:
        self.kind = kind
        size = self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        self.icon.setPixmap(theme_icon(self, kind).pixmap(size, size))
        self.label.setText(text)
        self.label.setToolTip(detail)
        if action:
            self.button.setText(action[0])
            self._slot = action[1]
            self.button.show()
        else:
            self._slot = None
            self.button.hide()
        self.setAccessibleName({"error": _("Fehler"), "warning": _("Hinweis"), "info": _("Information")}[kind])
        base = self.palette().color(QPalette.ColorRole.Window)
        accent = QColor(TONES[kind])
        self.setStyleSheet(f"#messageBar {{ background: {mix(base, accent, 0.14).name()}; "
                           f"border: 1px solid {mix(base, accent, 0.55).name()}; border-radius: 4px; }}")
        self.show()

    def clear(self) -> None:
        self.kind = ""
        self.label.clear()
        self.hide()

    def text(self) -> str:
        return self.label.text() if self.kind else ""
