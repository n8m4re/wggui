"""Stage 4 dialogs: new interface on a host, server-to-server tunnel between two interfaces."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from wggui import client, provision
from wggui.i18n import _
from wggui.ui.widgets import MessageBar, theme_icon


def _error_row(dialog: QDialog) -> tuple[QHBoxLayout, QLabel, QLabel]:
    icon = QLabel()
    size = dialog.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
    icon.setPixmap(theme_icon(dialog, "error").pixmap(size, size))
    text = QLabel()
    text.setWordWrap(True)
    row = QHBoxLayout()
    row.addWidget(icon)
    row.addWidget(text, 1)
    return row, icon, text


class NewInterfaceDialog(QDialog):
    """Name, address and port are prefilled with free values; conflicts are checked while typing."""

    def __init__(self, host_name: str, existing: dict[str, tuple[list[str], int | None]], systemd: bool,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Neues Interface – {host}").format(host=host_name))
        self.host_name, self.existing = host_name, existing
        name, address, port = provision.suggest_new_interface(existing)
        self.name = QLineEdit(name)
        self.address = QLineEdit(address)
        self.address.setToolTip(_("Adresse des Servers im neuen VPN-Netz, mit Netzmaske."))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(port)
        self.start = QCheckBox(_("Jetzt &starten"))
        self.start.setChecked(True)
        self.enable = QCheckBox(_("Beim &Booten starten (systemd)"))
        self.enable.setChecked(systemd)
        self.enable.setEnabled(systemd)
        form = QFormLayout()
        form.addRow(_("&Name:"), self.name)
        form.addRow(_("&Adresse:"), self.address)
        form.addRow(_("&ListenPort:"), self.port)
        form.addRow("", self.start)
        form.addRow("", self.enable)
        note = QLabel(_("Der private Schlüssel entsteht auf dem Server und verlässt ihn nie. Firewall und "
                        "Weiterleitung ändert die App nicht – den UDP-Port {port} ggf. selbst freigeben.")
                      .format(port=port))
        note.setWordWrap(True)
        self.note = note
        row, self.error_icon, self.error = _error_row(self)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText(_("Weiter…"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addLayout(row)
        layout.addWidget(buttons)
        for w in (self.name, self.address):
            w.textChanged.connect(self._validate)
        self.port.valueChanged.connect(self._validate)
        self._validate()

    def value(self) -> provision.NewInterface:
        name, address, port = self.name.text().strip(), self.address.text().strip(), self.port.value()
        provision.validate_new_interface(name, address, port, self.existing)
        return provision.NewInterface(self.host_name, name, address, port, start=self.start.isChecked(),
                                      enable=self.enable.isChecked())

    def _validate(self, *_args) -> bool:
        try:
            self.value()
            message = ""
        except ValueError as err:
            message = str(err)
        self.error.setText(message)
        self.error_icon.setVisible(bool(message))
        self.ok.setEnabled(not message)
        return not message

    def _accept(self) -> None:
        if self._validate():
            self.accept()


@dataclass(frozen=True)
class TunnelEnd:
    """One selectable end of a tunnel (an interface known to the app)."""

    host_id: int
    host_name: str
    iface: str
    public_key: str
    addresses: tuple[str, ...]
    endpoint: str

    @property
    def label(self) -> str:
        return f"{self.host_name} / {self.iface}"

    def tunnel_ip(self) -> str:
        for a in self.addresses:
            try:
                iface = ipaddress.ip_interface(a)
            except ValueError:
                continue
            return f"{iface.ip}/{iface.ip.max_prefixlen}"
        return ""


class TunnelDialog(QDialog):
    """Tunnel from the selected interface (A) to another interface known to the app (B)."""

    def __init__(self, own: TunnelEnd, others: list[TunnelEnd], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Server-Tunnel anlegen – {side}").format(side=own.label))
        self.own, self.others = own, others
        self.other = QComboBox()
        for end in others:
            self.other.addItem(end.label)
        self.endpoint_a = QLineEdit(own.endpoint)
        self.endpoint_b = QLineEdit()
        self.allowed_a = QLineEdit(own.tunnel_ip())
        self.allowed_b = QLineEdit()
        for w, tip in ((self.endpoint_a, _("Wie die Gegenseite diesen Server erreicht (Host:Port).")),
                       (self.allowed_a, _("Was zu diesem Server geroutet wird: seine Tunnel-Adresse und optional "
                                          "Netze dahinter, durch Komma getrennt."))):
            w.setToolTip(tip)
        self.psk = QCheckBox(_("&PresharedKey verwenden (zusätzlicher Schutz)"))
        self.psk.setChecked(True)
        self.keepalive = QCheckBox(_("Verbindung &offen halten (PersistentKeepalive 25 s)"))
        self.keepalive.setChecked(True)
        form = QFormLayout()
        form.addRow(_("&Gegenseite:"), self.other)
        form.addRow(_("Endpoint dieser Seite:"), self.endpoint_a)
        form.addRow(_("AllowedIPs dieser Seite:"), self.allowed_a)
        form.addRow(_("Endpoint der Gegenseite:"), self.endpoint_b)
        form.addRow(_("AllowedIPs der Gegenseite:"), self.allowed_b)
        form.addRow("", self.psk)
        form.addRow("", self.keepalive)
        hint = MessageBar(self)
        hint.show_message("info", _("Auf beiden Servern kommt je ein Peer-Block dazu – zwei Vorschauen nacheinander, "
                                    "jeweils mit Backup und Rollback. Firewall, Weiterleitung und Routen ändert die "
                                    "App nicht."))
        row, self.error_icon, self.error = _error_row(self)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText(_("Weiter…"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addLayout(row)
        layout.addWidget(buttons)
        self.other.currentIndexChanged.connect(self._other_changed)
        for w in (self.endpoint_a, self.endpoint_b, self.allowed_a, self.allowed_b):
            w.textChanged.connect(self._validate)
        self._other_changed()

    def _other_changed(self, *_args) -> None:
        end = self.others[self.other.currentIndex()] if self.others else None
        self.endpoint_b.setText(end.endpoint if end else "")
        self.allowed_b.setText(end.tunnel_ip() if end else "")
        self._validate()

    def value(self) -> tuple[provision.TunnelSide, provision.TunnelSide, TunnelEnd, bool, bool]:
        if not self.others:
            raise ValueError(_("Kein weiteres Interface in der App – zuerst einen zweiten Server übernehmen."))
        b = self.others[self.other.currentIndex()]
        if b.host_id == self.own.host_id and b.iface == self.own.iface:
            raise ValueError(_("Gegenseite muss ein anderes Interface sein."))

        def nets(text: str) -> tuple[str, ...]:
            values = tuple(v.strip() for v in text.split(",") if v.strip())
            if not values:
                raise ValueError(_("AllowedIPs fehlen."))
            for v in values:
                try:
                    ipaddress.ip_network(v, strict=False)
                except ValueError:
                    raise ValueError(_("AllowedIPs: {a} ist kein Netz.").format(a=v)) from None
            return values

        side_a = provision.TunnelSide(self.own.host_name, self.own.iface, self.own.public_key,
                                      client.validate_endpoint(self.endpoint_a.text()), nets(self.allowed_a.text()))
        side_b = provision.TunnelSide(b.host_name, b.iface, b.public_key,
                                      client.validate_endpoint(self.endpoint_b.text()), nets(self.allowed_b.text()))
        return side_a, side_b, b, self.psk.isChecked(), self.keepalive.isChecked()

    def _validate(self, *_args) -> bool:
        try:
            self.value()
            message = ""
        except ValueError as err:
            message = str(err)
        self.error.setText(message)
        self.error_icon.setVisible(bool(message))
        self.ok.setEnabled(not message)
        return not message

    def _accept(self) -> None:
        if self._validate():
            self.accept()
