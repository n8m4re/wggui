"""Dialogs for hosts, server imports, clients, write plans, QR codes, peers, and sync."""

from __future__ import annotations

import html
import io
import ipaddress
import logging
import os
from pathlib import Path

import segno
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wggui import client, sshkeys, sync
from wggui.apply import WritePlan
from wggui.db import NAME_RE, Change, Host, validate_host
from wggui.edit import PeerOp
from wggui.i18n import _, ngettext
from wggui.parse import PeerConf
from wggui.ui.widgets import MessageBar, muted, theme_icon, tone

log = logging.getLogger(__name__)


class HostDialog(QDialog):
    """Create or edit a host. Validate input as typed and enable Save only for valid values
    (KDE HIG "Getting input": show invalid input and disable confirmation)."""

    def __init__(self, parent: QWidget | None = None, host: Host | None = None, taken: set[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Host bearbeiten") if host else _("Host hinzufügen"))
        self.taken = (taken or set()) - ({host.name} if host else set())  # Names of other hosts
        self.name = QLineEdit(host.name if host else "")
        self.name.setPlaceholderText("vpn1")
        self.target = QLineEdit(host.ssh_target if host else "")
        self.target.setPlaceholderText("root@server.example.org")
        self.target.setToolTip(_("Login als user@host oder ein Alias aus ~/.ssh/config."))
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText(_("Standard"))
        self.port.setToolTip(_("Standard: Port aus ~/.ssh/config, sonst 22."))
        self.port.setValue((host.port or 0) if host else 0)
        self.sudo = QCheckBox(_("Befehle mit &sudo ausführen (Login ist nicht root)"))
        self.sudo.setChecked(bool(host and host.sudo))
        self.sudo.setToolTip(_("Nutzt sudo -n: Auf dem Host muss für diesen Nutzer sudo ohne Passwort "
                             "(NOPASSWD) eingerichtet sein."))
        # SSH key: system default (agent, ssh config); optional custom file (path only, never contents)
        self.key_system = QRadioButton(_("S&ystem (SSH-Agent, ssh-Config)"))
        self.key_file = QRadioButton(_("Eigene Schlüssel&datei:"))
        self.key_path = QLineEdit(host.identity_file if host and host.identity_file else "")
        self.key_path.setPlaceholderText(_("/home/…/id_ed25519 (private Datei, nicht .pub)"))
        self.key_choose = QPushButton(QIcon.fromTheme("document-open"), _("Wählen…"))
        self.key_new = QPushButton(QIcon.fromTheme("document-new"), _("Neu erzeugen…"))
        self.key_new.setToolTip(_("Neues ed25519-Paar im Datenordner der App (ohne Passphrase). Den Public Key "
                                "danach auf dem Server eintragen."))
        (self.key_file if host and host.identity_file else self.key_system).setChecked(True)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_path, 1)
        key_row.addWidget(self.key_choose)
        key_row.addWidget(self.key_new)
        self.key_choose.clicked.connect(self._choose_key)
        self.key_new.clicked.connect(self._new_key)
        self.key_file.toggled.connect(self._key_mode)
        self.key_path.textChanged.connect(self._edited)
        self.generated_public: str | None = None
        self.error_icon = QLabel()
        size = self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        self.error_icon.setPixmap(theme_icon(self, "error").pixmap(size, size))
        self.error = QLabel()
        self.error.setWordWrap(True)
        error_row = QHBoxLayout()
        error_row.addWidget(self.error_icon)
        error_row.addWidget(self.error, 1)

        form = QFormLayout()
        form.addRow("&Name:", self.name)  # addRow(str, field) makes the field a buddy → Alt+N
        form.addRow("SSH-&Ziel:", self.target)
        form.addRow("&Port:", self.port)
        form.addRow("", self.sudo)
        form.addRow(_("SSH-Schlüssel:"), self.key_system)
        form.addRow("", self.key_file)
        form.addRow("", key_row)
        hint = QLabel(_("Anmeldung über den SSH-Agent. Neuen Server vorher einmal im Terminal mit "
                      "<code>ssh &lt;ziel&gt;</code> verbinden (Host-Key bestätigen)."))
        hint.setToolTip(_("Login als root oder mit sudo ohne Passwort. Unterstützt: Debian/Ubuntu, Fedora/RHEL, "
                        "Arch, Alpine, openSUSE."))
        hint.setStyleSheet("color: palette(placeholder-text)")
        hint.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText(_("Speichern"))
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addLayout(error_row)
        layout.addWidget(self.buttons)

        self._key_mode()
        self._touched = host is not None  # New host: show errors only after typing starts
        for edit in (self.name, self.target):
            edit.textChanged.connect(self._edited)
        self.port.valueChanged.connect(self._edited)
        self._validate()

    def values(self) -> tuple[str, str, int | None, bool, str | None]:
        identity = self.key_path.text().strip() if self.key_file.isChecked() else None
        return self.name.text().strip(), self.target.text().strip(), self.port.value() or None, \
            self.sudo.isChecked(), identity or None

    def _key_mode(self, *_args) -> None:
        on = self.key_file.isChecked()
        for w in (self.key_path, self.key_choose, self.key_new):
            w.setEnabled(on)
        self._edited()

    def _choose_key(self) -> None:
        path = self.ask_key_file()
        if path:
            self.key_path.setText(path)

    def ask_key_file(self) -> str:
        start = self.key_path.text() or str(Path.home())
        path, _filter = QFileDialog.getOpenFileName(self, _("Private SSH-Schlüsseldatei wählen"), start)
        return path

    def _new_key(self) -> None:
        name = self.name.text().strip()
        if not NAME_RE.match(name):
            self.error.setText(_("Zuerst einen gültigen Namen eingeben – er bestimmt den Dateinamen des Schlüssels."))
            self.error_icon.show()
            return
        path = sshkeys.suggest_path(name)
        try:
            self.generated_public = sshkeys.generate(path, f"wggui-{name}")
        except ValueError as err:
            self.error.setText(str(err))
            self.error_icon.show()
            return
        self.key_path.setText(str(path))
        self.show_public_key(name, str(path), self.generated_public)

    def show_public_key(self, name: str, path: str, text: str) -> None:
        KeyDialog(name, self.target.text().strip(), path, text, self).exec()

    def _edited(self, *_args) -> None:
        self._touched = True
        self._validate()

    def _validate(self) -> bool:
        """Validate fields, show errors with an icon, and disable Save."""
        name, target, port, _sudo, _key = self.values()
        message = ""
        try:
            if not name:
                raise ValueError(_("Name fehlt."))
            if not target:
                raise ValueError(_("SSH-Ziel fehlt."))
            if name in self.taken:
                raise ValueError(_("Name: „{name}“ ist schon vergeben.").format(name=name))
            validate_host(name, target, port)
            if self.key_file.isChecked() and (problem := sshkeys.check_identity(self.key_path.text().strip())):
                raise ValueError(problem)
        except ValueError as err:
            message = str(err)
        visible = bool(message) and self._touched
        self.error.setText(message if visible else "")
        self.error_icon.setVisible(visible)
        self.ok.setEnabled(not message)
        return not message

    def _accept(self) -> None:
        self._touched = True
        if self._validate():
            self.accept()


class ImportDialog(QDialog):
    """Preview changes imported into the app from the server. The server is unchanged."""

    def __init__(self, host_name: str, changes: list[Change], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Vom Server übernehmen – {host_name}").format(host_name=host_name))
        self.resize(720, 420)
        intro = QLabel(_("Diese Änderungen werden aus <b>{v1}</b> in die App übernommen. Auf dem Server wird "
                         "nichts geändert.").format(v1=html.escape(host_name)))
        intro.setWordWrap(True)
        tree = QTreeWidget()
        tree.setHeaderLabels([_("Art"), _("Was"), _("Details")])
        tree.setRootIsDecorated(False)
        for c in changes:
            tree.addTopLevelItem(QTreeWidgetItem([_(c.kind), c.subject, c.detail]))
        for col in range(2):
            tree.resizeColumnToContents(col)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(_("In die App übernehmen"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(tree)
        layout.addWidget(buttons)


class ClientDialog(QDialog):
    """Create a client with only a required name; other fields have defaults (KDE HIG)."""

    def __init__(self, title: str, interface_addresses: list[str], used: list[str], suggested: list[str],
                 endpoint: str, dns: str, parent: QWidget | None = None, fixed_name: str | None = None):
        super().__init__(parent)
        self.rekey = fixed_name is not None  # Rekey existing peer: name and address stay fixed
        self.setWindowTitle(f"{'Neuen QR-Code ausstellen' if self.rekey else 'Client hinzufügen'} – {title}")
        self.interface_addresses, self.used = interface_addresses, used
        nets = ", ".join(str(n) for n in client.interface_networks(interface_addresses)) or "–"

        self.name = QLineEdit()
        self.name.setPlaceholderText(_("z. B. handy"))
        self.name.setToolTip(_("Steht als Kommentar über dem Peer in der Server-Config."))
        self.address = QLineEdit(", ".join(suggested))
        self.address.setToolTip(
            _("Adresse des Clients im VPN ({nets}). Vorschlag: erste freie Adresse.").format(nets=nets))
        self.split = QRadioButton(_("Nur das &VPN-Netz ({nets})").format(nets=nets))
        self.full = QRadioButton(_("&Gesamter Internetverkehr über den Server"))
        self.split.setChecked(True)
        self.full_hint = QLabel(_("Dafür muss der Server Weiterleitung und NAT erlauben (sysctl, Firewall). "
                                "Die App ändert das nicht."))
        self.full_hint.setWordWrap(True)
        self.full_hint.setVisible(False)
        self.full.toggled.connect(self.full_hint.setVisible)
        self.endpoint = QLineEdit(endpoint)
        self.endpoint.setPlaceholderText("vpn.example.org:51820")
        self.endpoint.setToolTip(_("Wie der Client den Server von außen erreicht (Hostname oder IP und Port)."))
        self.dns = QLineEdit(dns)
        self.dns.setPlaceholderText(_("optional, z. B. 10.98.0.1"))
        self.psk = QCheckBox(_("&PresharedKey verwenden (zusätzlicher Schutz)"))
        self.psk.setChecked(True)
        self.keepalive = QCheckBox(_("Verbindung &offen halten (PersistentKeepalive 25 s, für Handys hinter NAT)"))
        self.keepalive.setChecked(True)
        if self.rekey:
            self.name.setText(fixed_name or "")
            self.name.setReadOnly(True)
            self.address.setReadOnly(True)

        form = QFormLayout()  # Put the essentials first (KDE HIG "Simple by default")
        form.addRow("&Name:", self.name)
        form.addRow(_("Verkehr:"), self.split)
        form.addRow("", self.full)
        form.addRow("", self.full_hint)
        connection = QGroupBox(_("Verbindung (vorbelegt)"))
        conn_form = QFormLayout(connection)
        conn_form.addRow("&Adresse:", self.address)
        conn_form.addRow("&Endpoint:", self.endpoint)
        conn_form.addRow("&DNS:", self.dns)
        more = QGroupBox(_("Weitere Einstellungen"))
        more_layout = QVBoxLayout(more)
        more_layout.addWidget(self.psk)
        more_layout.addWidget(self.keepalive)

        self.error_icon = QLabel()
        size = self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        self.error_icon.setPixmap(theme_icon(self, "error").pixmap(size, size))
        self.error = QLabel()
        self.error.setWordWrap(True)
        error_row = QHBoxLayout()
        error_row.addWidget(self.error_icon)
        error_row.addWidget(self.error, 1)
        note = QLabel(_("Die Schlüssel des Clients entstehen hier auf dem Rechner und werden nicht gespeichert. "
                      "Im nächsten Schritt kommt die Vorschau der Änderung am Server."))
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholder-text)")
        self.rekey_warning = MessageBar(self)
        if self.rekey:
            self.rekey_warning.show_message(
                "warning", _("Der Peer bekommt neue Schlüssel. Das bisherige Gerät verbindet sich erst wieder, wenn "
                           "es den neuen QR-Code gescannt hat. Name, Adresse und übrige Einstellungen bleiben."))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText(_("Weiter…"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.rekey_warning)
        layout.addLayout(form)
        layout.addWidget(connection)
        layout.addWidget(more)
        layout.addWidget(note)
        layout.addLayout(error_row)
        layout.addWidget(buttons)

        self._touched = False
        for edit in (self.name, self.address, self.endpoint, self.dns):
            edit.textChanged.connect(self._edited)
        self._validate()

    def spec(self) -> client.ClientSpec:
        """Return validated input or raise ValueError with a clear message."""
        if self.rekey:  # Name comes from the server (display only); use the peer's current address
            name = self.name.text()
        else:
            name = client.validate_name(self.name.text()) if self.name.text().strip() else ""
            if not name:
                raise ValueError(_("Name fehlt."))
        addresses = client.validate_addresses(
            [a.strip() for a in self.address.text().split(",") if a.strip()], self.interface_addresses, self.used)
        return client.ClientSpec(name, addresses, client.validate_endpoint(self.endpoint.text()),
                                 self.full.isChecked(), client.validate_dns(self.dns.text()),
                                 self.psk.isChecked(), self.keepalive.isChecked())

    def _edited(self, *_args) -> None:
        self._touched = True
        self._validate()

    def _validate(self) -> bool:
        try:
            self.spec()
            message = ""
        except ValueError as err:
            message = str(err)
        visible = bool(message) and self._touched
        self.error.setText(message if visible else "")
        self.error_icon.setVisible(visible)
        self.ok.setEnabled(not message)
        return not message

    def _accept(self) -> None:
        self._touched = True
        if self._validate():
            self.accept()


class PlanDialog(QDialog):
    """Preview warnings, diff, and steps before writing to the server; label the action clearly."""

    def __init__(self, plan: WritePlan, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Änderung auf {plan_host_name} schreiben").format(plan_host_name=plan.host_name))
        self.resize(720, 520)
        layout = QVBoxLayout(self)
        for warning in plan.warnings:
            bar = MessageBar(self)
            bar.show_message("warning", warning)
            layout.addWidget(bar)
        head = QLabel(f'<span style="font-size:large; font-weight:600">{html.escape(plan.action)}</span><br>'
                      f"{html.escape(plan.host_name)} · <code>{html.escape(plan.conf_path)}</code>")
        head.setWordWrap(True)
        diff_title = QLabel(_("Vorschau – „+“ kommt dazu, „-“ fällt weg:"))
        diff = QPlainTextEdit()
        diff.setReadOnly(True)
        diff.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        diff.setAccessibleName(_("Änderung (Diff)"))
        colors = {"+": tone(self, "positive").name(), "-": tone(self, "error").name(), "@": muted(self)}
        for line in plan.preview.splitlines():
            color = colors.get(line[:1])  # Supplement +/- with color; do not rely on color alone
            text = html.escape(line).replace(" ", "&nbsp;")
            diff.appendHtml(f'<span style="color:{color}">{text}</span>' if color else text)
        diff.moveCursor(diff.textCursor().MoveOperation.Start)
        runtime = _("Tunnel läuft weiter (wg syncconf)") if plan.running else _("nur Datei, Interface läuft nicht")
        summary = QLabel(_("Ablauf: Backup vorher · Rücknahme nach {seconds} s, falls keine Bestätigung ankommt · "
                           "{runtime}").format(seconds=plan.rollback_seconds, runtime=runtime))
        summary.setWordWrap(True)
        self.steps = QLabel("<ol style='margin:0'>" + "".join(f"<li>{html.escape(x)}</li>" for x in plan.steps())
                            + "</ol>")
        self.steps.setWordWrap(True)
        self.steps.hide()
        toggle = QToolButton()
        toggle.setText(_("Ablauf anzeigen"))
        toggle.setCheckable(True)
        toggle.setArrowType(Qt.ArrowType.RightArrow)
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toggle.setAutoRaise(True)

        def show_steps(on: bool) -> None:
            self.steps.setVisible(on)
            toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
            toggle.setText(_("Ablauf ausblenden") if on else _("Ablauf anzeigen"))

        toggle.toggled.connect(show_steps)
        steps_row = QHBoxLayout()
        steps_row.addWidget(summary, 1)
        steps_row.addWidget(toggle)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText(_("Auf {plan_host_name} schreiben").format(plan_host_name=plan.host_name))
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.ok.setAutoDefault(False)
        cancel.setDefault(True)  # Enter cancels; writing requires an explicit click
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(head)
        layout.addWidget(diff_title)
        layout.addWidget(diff, 1)
        layout.addLayout(steps_row)
        layout.addWidget(self.steps)
        layout.addWidget(buttons)


def qr_png(text: str, size: int = 320) -> bytes:
    """Encode the config as a PNG QR code (segno, error correction M); WireGuard apps read it as text."""
    qr = segno.make(text, error="m", micro=False)
    modules = qr.symbol_size(scale=1)[0]
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=max(2, size // modules), border=4)
    return buf.getvalue()


class QrDialog(QDialog):
    """Show the client config as a QR code in memory; confirm closing without saving."""

    def __init__(self, name: str, config: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Client {name} – QR-Code").format(name=name))
        self._config = config
        self.saved_to: str | None = None
        self.name = name
        image = QLabel()
        pixmap = QPixmap()
        pixmap.loadFromData(qr_png(config), "PNG")
        image.setPixmap(pixmap)
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setAccessibleName(_("QR-Code mit der Config für {name}").format(name=name))
        text = QLabel(_('<span style="font-size:large; font-weight:600">Jetzt mit der WireGuard-App scannen'
                      "</span><br>In der App „Aus QR-Code erstellen“ wählen. Alternativ als Datei speichern."))
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.key_note = MessageBar(self)
        self.key_note.show_message("warning", _("Enthält den privaten Schlüssel des Clients und wird nicht "
                                                "gespeichert. Nach dem Schließen ist die Config weg."))
        text.setWordWrap(True)
        self.save_error = MessageBar(self)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        save = buttons.addButton(_("Als Datei &speichern…"), QDialogButtonBox.ButtonRole.ActionRole)
        save.setIcon(QIcon.fromTheme("document-save-as"))
        save.clicked.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(image)
        layout.addWidget(self.key_note)
        layout.addWidget(self.save_error)
        layout.addWidget(buttons)

    def save(self) -> bool:
        path = self.ask_save_path(f"{self.name}.conf")
        if not path:
            return False
        try:
            write_private_file(path, self._config)
        except OSError as err:
            log.warning("Client-Config nicht gespeichert: %s", err.strerror or err)
            self.save_error.show_message("error", _("Nicht gespeichert: {reason}. Anderen Ordner wählen – die Config "
                                                    "ist weiter nur hier im Fenster.")
                                                        .format(reason=err.strerror or err))
            return False
        self.save_error.clear()
        self.saved_to = path
        return True

    def ask_save_path(self, suggestion: str) -> str:
        path, _filter = QFileDialog.getSaveFileName(self, _("Client-Config speichern"), suggestion,
                                              _("WireGuard-Config (*.conf)"))
        return path

    def confirm_discard(self) -> bool:
        answer = QMessageBox.question(
            self, _("Config verwerfen?"),
            _("Die Config wurde nicht gespeichert. Nach dem Schließen ist der private Schlüssel weg.\n\n"
            "Wurde der QR-Code schon gescannt?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def reject(self) -> None:
        if self.saved_to is None and not self.confirm_discard():
            return
        self._config = ""
        super().reject()


def write_private_file(path: str, text: str) -> None:
    """Keep the file readable only by its owner (0600), even if it already exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(text)


class PeerDialog(QDialog):
    """Edit a peer's name, AllowedIPs, and keepalive; leave server keys and PSK unchanged."""

    def __init__(self, title: str, peer: PeerConf, others: dict[str, list[str]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Peer bearbeiten – {title}").format(title=title))
        self.peer, self.others = peer, others
        self.name = QLineEdit(peer.name or "")
        self.name.setPlaceholderText(_("leer = Name unverändert lassen"))
        self.allowed = QLineEdit(", ".join(peer.allowed_ips))
        self.allowed.setToolTip(_("Adressen/Netze, die zu diesem Peer geroutet werden, durch Komma getrennt."))
        self.keepalive = QSpinBox()
        self.keepalive.setRange(0, 65535)
        self.keepalive.setSpecialValueText("aus")
        self.keepalive.setSuffix(" s")
        self.keepalive.setValue(peer.keepalive or 0)
        key = QLabel(f"<code>{html.escape(peer.public_key)}</code>")
        key.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        form = QFormLayout()
        form.addRow("&Name:", self.name)
        form.addRow("&AllowedIPs:", self.allowed)
        form.addRow("&Keepalive:", self.keepalive)
        details = QGroupBox(_("Peer-Details (unverändert)"))
        details_form = QFormLayout(details)
        key.setWordWrap(True)
        details_form.addRow("PublicKey:", key)
        if peer.endpoint:
            details_form.addRow(_("Endpoint:"), QLabel(html.escape(peer.endpoint)))
        note = QLabel(_("Schlüssel und PresharedKey bleiben unverändert. Im nächsten Schritt kommt die Vorschau."))
        note.setWordWrap(True)
        self.error_icon = QLabel()
        size = self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        self.error_icon.setPixmap(theme_icon(self, "error").pixmap(size, size))
        self.error = QLabel()
        self.error.setWordWrap(True)
        error_row = QHBoxLayout()
        error_row.addWidget(self.error_icon)
        error_row.addWidget(self.error, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText(_("Weiter…"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(details)
        layout.addWidget(note)
        layout.addLayout(error_row)
        layout.addWidget(buttons)
        for edit in (self.name, self.allowed):
            edit.textChanged.connect(self._validate)
        self.keepalive.valueChanged.connect(self._validate)
        self._validate()

    def op(self) -> PeerOp:
        name = client.validate_name(self.name.text()) if self.name.text().strip() else ""
        allowed = [a.strip() for a in self.allowed.text().split(",") if a.strip()]
        if not allowed:
            raise ValueError(_("AllowedIPs fehlen."))
        for a in allowed:
            try:
                ipaddress.ip_network(a, strict=False)
            except ValueError:
                raise ValueError(_("AllowedIPs: {a} ist kein Netz.").format(a=a)) from None
        if hit := client.overlapping(allowed, self.others):
            raise ValueError(_("AllowedIPs überschneiden sich mit {hit}.").format(hit=hit))
        op = PeerOp("edit", self.peer.public_key, name, tuple(allowed), self.keepalive.value() or None)
        op.line()  # Same validation as for the host operation
        return op

    def _validate(self, *_args) -> bool:
        try:
            self.op()
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


class SyncDialog(QDialog):
    """Sync app and server: one row per difference with a direction choice; default to no writes."""

    def __init__(self, title: str, drifts: list[sync.Drift], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Mit Server abgleichen – {title}").format(title=title))
        self.drifts = drifts
        intro = QLabel(_("Je Zeile die Richtung wählen. „In die App …“ ändert nur die App; alles, was den Server "
                       "ändert, kommt danach gesammelt in eine Vorschau mit Backup und Rollback."))
        intro.setWordWrap(True)
        self.table = QTableWidget(len(drifts), 3)
        self.table.setHorizontalHeaderLabels([_("Peer"), _("Abweichung"), _("Aktion")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        self.boxes: list[QComboBox] = []
        for row, d in enumerate(drifts):
            self.table.setItem(row, 0, QTableWidgetItem(d.label))
            detail = QTableWidgetItem(d.detail)
            detail.setToolTip(d.detail)
            self.table.setItem(row, 1, detail)
            box = QComboBox()
            for choice in d.choices:  # Translate labels, keep IDs as data (sync.* compares IDs)
                box.addItem(_(choice), choice)
            box.setCurrentIndex(box.findData(d.default))
            box.setAccessibleName(_("Aktion für {d_label}").format(d_label=d.label))
            box.currentTextChanged.connect(self._summary)
            self.table.setCellWidget(row, 2, box)
            self.boxes.append(box)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.resizeRowsToContents()
        rows = sum(self.table.rowHeight(r) for r in range(len(drifts)))
        frame = self.table.horizontalHeader().height() + 2 * self.table.frameWidth() + 24
        self.table.setMinimumHeight(min(360, rows + frame))
        self.resize(900, 0)  # Height follows content
        self.summary = QLabel()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summary)
        layout.addWidget(buttons)
        self._summary()

    def choices(self) -> dict[int, str]:
        return {i: box.currentData() for i, box in enumerate(self.boxes)}

    def _summary(self, *_args) -> None:
        picked = list(self.choices().values())
        server = sum(c in (sync.TO_SERVER, sync.ADD_SERVER, sync.REMOVE_SERVER, sync.SYNC_RUNTIME) for c in picked)
        app = sum(c in (sync.TO_APP, sync.REMOVE_APP) for c in picked)
        app_text = ngettext("{n} Änderung", "{n} Änderungen", app).format(n=app)
        server_text = ngettext("{n} Änderung", "{n} Änderungen", server).format(n=server)
        self.summary.setText(_("App: {app} · Server: {server}").format(app=app_text, server=server_text))
        self.ok.setText(_("Weiter zur Vorschau…") if server else _("In die App übernehmen"))
        self.ok.setEnabled(bool(server or app))


class KeyDialog(QDialog):
    """Show a host's public SSH key for copying and explain how to install it on the server."""

    def __init__(self, name: str, target: str, path: str, public_key: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("SSH-Public-Key – {name}").format(name=name))
        self.resize(640, 0)
        head = QLabel(_('<span style="font-size:large; font-weight:600">Public Key auf dem Server eintragen</span>'
                      "<br>Diese Zeile gehört in <code>authorized_keys</code> des Login-Nutzers auf dem Server. "
                      "Der private Schlüssel bleibt hier und wird nicht angezeigt."))
        head.setWordWrap(True)
        self.key_text = QPlainTextEdit(public_key)
        self.key_text.setReadOnly(True)
        self.key_text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.key_text.setMaximumHeight(80)
        self.key_text.setAccessibleName(_("Öffentlicher SSH-Schlüssel"))
        target_text = html.escape(target or "<ziel>")
        how = QLabel(_("Mit Passwort-Login einmal im Terminal: <code>ssh-copy-id -i {path}.pub {target}</code><br>"
                       "Sonst die Zeile oben von Hand eintragen.").format(path=html.escape(path), target=target_text))
        how.setWordWrap(True)
        how.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton(_("Public Key &kopieren"), QDialogButtonBox.ButtonRole.ActionRole)
        copy.setIcon(QIcon.fromTheme("edit-copy"))
        copy.clicked.connect(self.copy)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(head)
        layout.addWidget(self.key_text)
        layout.addWidget(how)
        layout.addWidget(buttons)

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.key_text.toPlainText())
