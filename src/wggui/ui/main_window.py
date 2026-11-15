"""Main window: hosts and interfaces on the left; messages, details, and peer status on the right.

Refresh reads all hosts over SSH. Import from server updates only the local database after a preview.
Add client (stage 2) writes to the server only through a plan in apply.py.
The log is docked below (Ctrl+L) and opens automatically on errors.

Follow the KDE HIG (develop.kde.org/hig): show SSH progress with an icon and status bar,
show messages in the window, use icons and text for state, and omit Remove from the toolbar.
"""

from __future__ import annotations

import contextlib
import html
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QFont, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wggui import apply, client, edit, fmt, i18n, keys, provision, settings, sshkeys, sync
from wggui.db import Change, Database, Host
from wggui.discover import WG_DIR, ConnectionInfo, HostState, check_connection, discover
from wggui.i18n import N_, _, ngettext
from wggui.parse import InterfaceConf, PeerConf, RuntimePeer, parse_conf
from wggui.remote import COMMAND_FAILED, Runner, SshRunner
from wggui.ui.dialogs import (
    ClientDialog,
    HostDialog,
    ImportDialog,
    KeyDialog,
    PeerDialog,
    PlanDialog,
    QrDialog,
    SyncDialog,
)
from wggui.ui.log_view import LogDock
from wggui.ui.provision_dialogs import NewInterfaceDialog, TunnelDialog, TunnelEnd
from wggui.ui.widgets import MessageBar, muted, theme_icon, tone
from wggui.ui.workers import Background

ROLE = Qt.ItemDataRole.UserRole
PEER_COLUMNS = [N_("Name"), N_("Status"), "AllowedIPs", N_("Endpoint"), N_("Letzter Handshake"),
                N_("Empfangen"), N_("Gesendet"), "PublicKey"]
KEY_COLUMN = PEER_COLUMNS.index("PublicKey")
AUTO_REFRESH_MS = 60_000
NOT_IMPORTED = N_("nicht übernommen")

log = logging.getLogger(__name__)


# build(host, iface, state, conf) → (operations, sync runtime?, description, warnings, after success)
# or None to cancel. "after success" runs only after the server write is confirmed.
BuildResult = tuple[list[edit.PeerOp], bool, str, list[str], Callable[[], None] | None]
Builder = Callable[[Host, str, HostState, InterfaceConf], BuildResult | None]


@dataclass
class PeerRow:
    name: str | None
    public_key: str
    allowed_ips: list[str]
    endpoint: str | None
    runtime: RuntimePeer | None
    note: str = ""
    disabled: bool = False


def _default_runner(host: Host) -> Runner:
    return SshRunner(host.ssh_target, host.port, sudo=host.sudo, identity_file=host.identity_file)


class _SortItem(QTableWidgetItem):
    """Table cell with numeric sorting, such as byte counts or handshake times."""

    def __init__(self, text: str, rank: int | None = None):
        super().__init__(text)
        self.rank = rank

    def __lt__(self, other: QTableWidgetItem) -> bool:  # Qt docs: QTableWidgetItem::operator<
        if self.rank is not None and isinstance(other, _SortItem) and other.rank is not None:
            return self.rank < other.rank
        return self.text().lower() < other.text().lower()


def _fill(widget: QWidget, actions: list[QAction | None]) -> None:
    """Fill a toolbar or menu; None inserts a separator."""
    for act in actions:
        if act is None:
            widget.addSeparator()
        else:
            widget.addAction(act)


def peer_status(runtime: RuntimePeer | None, running: bool, disabled: bool = False) -> tuple[str, str, str]:
    """Return (text, icon name, tooltip) for Status; text conveys meaning without color."""
    if disabled:
        return (_("gesperrt"), "object-locked",
                _("Gesperrt: in der Config auskommentiert (#wggui-off), kann sich nicht verbinden."))
    if runtime is None:
        if running:
            return (_("nicht geladen"), "dialog-warning",
                    _("Peer steht in der Datei, ist im laufenden Interface aber nicht aktiv."))
        return "–", "", _("Interface läuft nicht oder wurde noch nicht gelesen.")
    if runtime.latest_handshake <= 0:
        return _("nie verbunden"), "network-offline", _("Noch kein Handshake seit dem Start des Interfaces.")
    if fmt.is_active(runtime.latest_handshake):
        return _("aktiv"), "network-transmit-receive", _("Handshake in den letzten 3 Minuten.")
    return _("offline"), "network-offline", _("Letzter Handshake vor mehr als 3 Minuten: Peer vermutlich offline.")


def short_error(message: str) -> str:
    """Format a message-bar error with guidance first; command and exit code stay in the log."""
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return _("Unbekannter Fehler")
    hint = next((line.lstrip("→ ").strip() for line in lines if line.startswith("→")), "")
    head = lines[0]
    marker = _(COMMAND_FAILED)  # Prefix of remote.CommandError (translated the same way)
    if f": {marker}" in head:
        prefix = head.split(f": {marker}", 1)[0]
    elif head.startswith(marker):
        prefix = ""
    else:
        prefix = head
    cause = next((line for line in lines[1:] if not line.startswith("→")), "")[:140]
    if not hint and not cause:
        return head[:160]
    if not hint:
        hint = _("Verbindung testen oder Details im Protokoll ansehen")
    parts = [x for x in (prefix, hint) if x]
    if not parts:
        return cause or head[:160]
    text = ". ".join(p.rstrip(".") for p in parts) + "."
    return f"{text} ({cause})" if cause else text


def connection_problems(info: ConnectionInfo) -> list[str]:
    problems = []
    if not info.is_root:
        problems.append(_("Kein root-Zugriff: als root anmelden oder im Host „sudo“ einschalten (Host bearbeiten…)."))
    if not info.wg_installed:
        problems.append(_("wireguard-tools fehlen. Installieren: {info_install_hint}")
            .format(info_install_hint=info.install_hint))
    return problems


class MainWindow(QMainWindow):
    def __init__(self, db: Database, runner_factory: Callable[[Host], Runner] = _default_runner,
                 confirm_import: Callable[[str, list[Change]], bool] | None = None,
                 log_path: Path | None = None):
        super().__init__()
        self.db = db
        self.runner_factory = runner_factory
        self.confirm_import = confirm_import or self._confirm_import_dialog
        self.states: dict[int, HostState] = {}
        self.errors: dict[int, str] = {}
        self.read_at: dict[int, datetime] = {}
        self.checks: dict[int, tuple[ConnectionInfo, datetime]] = {}
        self.rows: list[PeerRow] = []  # Peer table rows for peer actions
        self._peer_key: str | None = None  # Selected peer survives table rebuilds
        self._rebuilding = False
        self.busy: dict[int, str] = {}  # host_id → "Reading vpn1" / "Writing to vpn1" during SSH
        self.flow_errors: dict[int, str] = {}  # Client, peer, or sync flow error per host
        self.round: dict[str, bool] = {}  # Current round results (hostname → success)
        self.notice = ""  # Last action result, appended to the summary
        self.bg = Background()
        self.setWindowTitle("WGGUI")
        self.resize(1100, 640)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName(_("Hosts und Interfaces"))
        self.tree.itemSelectionChanged.connect(self.show_details)
        self.tree.itemDoubleClicked.connect(self._tree_double_clicked)

        self.bar = MessageBar()  # Host errors
        self.hints = MessageBar()  # Hints (no root, missing wg, …)
        self.info = QLabel()
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.LinksAccessibleByMouse
                                          | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.info.setContentsMargins(4, 6, 4, 8)
        self.info.linkActivated.connect(self._info_link)
        self._iface_pubkey: str | None = None
        self.empty_button = QPushButton(QIcon.fromTheme("list-add"), _("Host hinzufügen…"))
        self.empty_button.clicked.connect(self.add_host)
        self.empty_button.setMinimumHeight(36)
        self.empty = QWidget()  # Empty state (KDE HIG): centered icon, explanation, and one button
        empty_icon = QLabel()
        empty_icon.setPixmap(QIcon.fromTheme("network-vpn", self.style().standardIcon(
            self.style().StandardPixmap.SP_DriveNetIcon)).pixmap(64, 64))
        self.empty_text = QLabel(_("<h2>Noch kein Host eingetragen</h2>Ein Host ist ein Linux-Server mit WireGuard, "
                                 "den die App per SSH liest.<br>Mit <b>Host hinzufügen…</b> (Strg+N) beginnen."))
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_text.setWordWrap(True)
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.addStretch(1)
        empty_layout.addWidget(empty_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(self.empty_text)
        empty_layout.addWidget(self.empty_button, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch(2)
        self.drift_bar = MessageBar()  # App ↔ server differences with a Sync button
        self.table = QTableWidget(0, len(PEER_COLUMNS))
        self.table.setAccessibleName(_("Peers"))
        self.table.setHorizontalHeaderLabels([_(c) for c in PEER_COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        # Default: config order; clicking a column sorts (-1 = no sort indicator)
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self._table_selection_changed)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(_("Peers filtern: Name, IP oder Schlüssel (Strg+F)"))
        self.filter.setClearButtonEnabled(True)
        self.filter.setAccessibleName(_("Peers filtern"))
        self.filter.textChanged.connect(self._apply_filter)

        right = QWidget()
        layout = QVBoxLayout(right)
        layout.addWidget(self.bar)
        layout.addWidget(self.hints)
        layout.addWidget(self.info)
        layout.addWidget(self.drift_bar)
        layout.addWidget(self.empty, 1)
        layout.addWidget(self.filter)
        layout.addWidget(self.table, 1)
        layout.addStretch(0)
        splitter = QSplitter()
        splitter.addWidget(self.tree)
        splitter.addWidget(right)
        splitter.setSizes([260, 840])
        self.setCentralWidget(splitter)
        self.log_dock = LogDock(self, log_path, db.path)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_dock.hide()

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # Indeterminate while SSH is running
        self.progress.setMaximumWidth(120)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName(_("SSH läuft"))
        self.statusBar().addPermanentWidget(self.progress)
        self.progress.hide()

        self.auto_timer = QTimer(self)
        self.auto_timer.setInterval(AUTO_REFRESH_MS)
        self.auto_timer.timeout.connect(self._auto_refresh)
        self._build_actions()
        self.reload_tree()
        self.set_auto_refresh(self.act_auto.isChecked())

    # --- Setup ----------------------------------------------------------------------------------

    def _action(self, text: str, icon: str, slot: Callable[[], None], shortcut: Any = None,
                tip: str = "") -> QAction:
        action = QAction(QIcon.fromTheme(icon), text, self)
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setToolTip(tip or text.rstrip("…"))
        action.setData(action.toolTip())  # Base tooltip; _update_actions appends why it is disabled
        return action

    def _build_actions(self) -> None:
        self.act_add = self._action(_("Host hinzufügen…"), "list-add", self.add_host, QKeySequence.StandardKey.New,
                                    _("Linux-Server eintragen, den die App per SSH liest"))
        self.act_edit = self._action(_("Host bearbeiten…"), "document-edit", self.edit_host,
                                     tip=_("Name, SSH-Ziel, Port oder sudo ändern"))
        self.act_delete = self._action(_("Host entfernen…"), "list-remove", self.delete_host,
                                       QKeySequence.StandardKey.Delete,
                                       _("Host aus der App entfernen – auf dem Server ändert sich nichts"))
        self.act_delete.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)  # Delete only in host list
        self.act_pubkey = self._action(_("SSH-Public-Key anzeigen…"), "view-certificate", self.show_host_key,
                                       tip=_("Öffentlichen Teil des eigenen SSH-Schlüssels dieses Hosts anzeigen und "
                                           "kopieren (zum Eintragen auf dem Server)"))
        self.act_install = self._action(_("wireguard-tools installieren…"), "run-install", self.install_tools,
                                        tip=_("wireguard-tools mit dem Paketmanager des Servers installieren"))
        self.act_new_iface = self._action(_("Neues Interface…"), "network-vpn", self.new_interface,
                                          tip=_("Neues WireGuard-Interface auf diesem Server anlegen – Schlüssel "
                                                "entsteht auf dem Server"))
        self.act_tunnel = self._action(_("Server-Tunnel anlegen…"), "network-connect", self.new_tunnel,
                                       tip=_("Tunnel zwischen diesem Interface und einem Interface auf einem anderen "
                                             "Server anlegen (beide Seiten)"))
        self.act_test = self._action(_("Verbindung testen"), "network-connect", self.test_connection,
                                     tip=_("SSH-Login, root/sudo und wireguard-tools prüfen"))
        self.act_refresh = self._action(_("Aktualisieren"), "view-refresh", self.refresh_all,
                                        QKeySequence.StandardKey.Refresh, _("Status aller Hosts per SSH lesen"))
        self.act_import = self._action(_("Vom Server übernehmen…"), "document-import", self.import_from_server,
                                       tip=_("Configs des Hosts in die App übernehmen – vorher Vorschau"))
        self.act_client = self._action(_("Client hinzufügen…"), "list-add-user", self.add_client,
                                       tip=_("Neuen Client (Handy, Laptop) an diesem Interface anlegen, mit QR-Code"))
        self.act_sync = self._action(_("Mit Server abgleichen…"), "view-refresh-symbolic", self.sync_interface,
                                     tip=_("Abweichungen App ↔ Server-Datei ↔ Laufzeit anzeigen, Richtung je Peer "
                                           "wählen"))
        self.act_backups = self._action(_("Alte Backups aufräumen…"), "edit-clear-history", self.prune_backups,
                                        tip=_("Ältere Sicherungen der Config auf dem Server löschen – die neuesten "
                                              "{n} bleiben").format(n=apply.KEEP_BACKUPS))
        self.act_peer_edit = self._action(_("Peer bearbeiten…"), "document-edit", self.edit_peer,
                                          tip=_("Name, AllowedIPs oder Keepalive auf dem Server ändern – mit Vorschau"))
        self.act_peer_toggle = self._action(_("Peer sperren…"), "object-locked", self.toggle_peer,
                                            tip=_("Peer auf dem Server auskommentieren bzw. wieder freigeben"))
        self.act_peer_rekey = self._action(_("Neuen QR-Code ausstellen…"), "view-barcode-qr", self.rekey_peer,
                                           tip=_("Neue Schlüssel für diesen Client und QR-Code – das alte Gerät muss "
                                               "neu scannen"))
        self.act_peer_remove = self._action(_("Peer entfernen…"), "list-remove-user", self.remove_peer,
                                            QKeySequence.StandardKey.Delete,
                                            _("Peer aus der Server-Config löschen – mit Vorschau und Backup"))
        self.act_peer_remove.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)  # Delete only in peer table
        self.act_copy_key = self._action(_("PublicKey kopieren"), "edit-copy", self.copy_public_key,
                                         QKeySequence.StandardKey.Copy)
        self.act_copy_key.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.act_find = self._action(_("Peers filtern"), "edit-find", self._focus_filter, QKeySequence.StandardKey.Find)
        self.addAction(self.act_find)
        self.act_log = self.log_dock.toggleViewAction()
        self.act_log.setText(_("Protokoll"))
        self.act_log.setIcon(QIcon.fromTheme("utilities-log-viewer"))
        self.act_log.setShortcut(QKeySequence("Ctrl+L"))
        self.act_log.setToolTip(_("Protokoll anzeigen (Strg+L)"))

        bar = QToolBar(_("Aktionen"))
        bar.setObjectName("aktionen")
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar.setMovable(False)
        # Toolbar holds common actions (KDE HIG); Edit/Test/Remove go in menus and context menus
        self.act_sync.setIconText(_("Abgleichen"))
        _fill(bar, [self.act_add, None, self.act_refresh, self.act_import, None, self.act_client, self.act_sync,
                    None, self.act_log])
        self.addToolBar(bar)
        _fill(self.menuBar().addMenu(_("&Host")), [self.act_add, self.act_edit, self.act_delete, None, self.act_test,
                                                self.act_refresh, self.act_import, None, self.act_install,
                                                   self.act_new_iface, None, self.act_pubkey])
        _fill(self.menuBar().addMenu(_("&Interface")), [self.act_client, self.act_sync, self.act_tunnel, None,
                                                        self.act_backups])
        _fill(self.menuBar().addMenu(_("&Peer")), [self.act_peer_edit, self.act_peer_rekey, self.act_peer_toggle,
                                                self.act_peer_remove, None, self.act_copy_key])
        view = self.menuBar().addMenu(_("&Ansicht"))
        view.addAction(self.act_log)
        view.addSeparator()
        language_menu = view.addMenu(_("Sprache"))
        group = QActionGroup(self)
        current = self.language_setting()
        for code, label in [("system", _("Systemsprache")), *i18n.LANGUAGES.items()]:
            act = QAction(label, self, checkable=True)
            act.setChecked(code == current)
            act.setData(code)
            act.triggered.connect(lambda _checked=False, c=code: self.choose_language(c))
            group.addAction(act)
            language_menu.addAction(act)
        view.addSeparator()
        self.act_auto = QAction(_("Automatisch aktualisieren (jede Minute)"), self, checkable=True)
        self.act_auto.setChecked(settings.get_bool("auto_refresh"))
        self.act_auto.toggled.connect(self.set_auto_refresh)
        view.addAction(self.act_auto)
        view.addSeparator()
        self.act_col_traffic = QAction(_("Spalten Empfangen/Gesendet"), self, checkable=True)
        self.act_col_key = QAction(_("Spalte PublicKey"), self, checkable=True)
        for act in (self.act_col_traffic, self.act_col_key):
            act.toggled.connect(self._apply_columns)
            view.addAction(act)
        self._apply_columns()

        # Right-click menus and shortcuts scoped to each widget
        separator = QAction(self)
        separator.setSeparator(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        separator2 = QAction(self)
        separator2.setSeparator(True)
        self.tree.addActions([self.act_client, self.act_sync, separator2, self.act_edit, self.act_test,
                              self.act_import, separator, self.act_delete])
        separator3 = QAction(self)
        separator3.setSeparator(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self.table.addActions([self.act_peer_edit, self.act_peer_rekey, self.act_peer_toggle, self.act_peer_remove,
                               separator3, self.act_copy_key])
        self._update_actions()

    def _update_actions(self) -> None:
        host = self.selected_host()
        idle = host is not None and host.id not in self.busy
        reason = "" if idle else (_("wird gerade gelesen") if host else _("zuerst einen Host auswählen"))
        for act in (self.act_edit, self.act_delete, self.act_test, self.act_import, self.act_install,
                    self.act_new_iface):
            act.setEnabled(idle)
            act.setToolTip(act.data() if idle else f"{act.data()} ({reason})")
        has_key = host is not None and bool(host.identity_file)
        self.act_pubkey.setEnabled(has_key)
        self.act_pubkey.setToolTip(self.act_pubkey.data() if has_key else
                                   _("{v1} (Host nutzt das System – Schlüssel im SSH-Agent)")
                                       .format(v1=self.act_pubkey.data()))
        reason = self._client_blocked()
        for act in (self.act_client, self.act_sync, self.act_backups, self.act_tunnel):
            act.setEnabled(not reason)
            act.setToolTip(act.data() if not reason else f"{act.data()} ({reason})")
        row = self._selected_row()
        reason = self._client_blocked() or (
            _("zuerst einen Peer in der Tabelle auswählen") if row is None else
            _("Peer steht nur in der Laufzeit, nicht in der Datei – „Mit Server abgleichen…“") if row.note else "")
        for act in (self.act_peer_edit, self.act_peer_rekey, self.act_peer_toggle, self.act_peer_remove):
            act.setEnabled(not reason)
            act.setToolTip(act.data() if not reason else f"{act.data()} ({reason})")
        self.act_peer_toggle.setText(_("Peer entsperren…") if row is not None and row.disabled else _("Peer sperren…"))
        hosts = self.db.hosts()
        self.act_refresh.setEnabled(any(h.id not in self.busy for h in hosts))
        self.act_copy_key.setEnabled(self._selected_key() is not None)

    # --- Selection -----------------------------------------------------------------------------

    def _selected_data(self) -> tuple | None:
        items = self.tree.selectedItems()
        return items[0].data(0, ROLE) if items else None

    def selected_host(self) -> Host | None:
        data = self._selected_data()
        if not data:
            return None
        try:
            return self.db.host(data[1])
        except KeyError:
            return None

    def _client_blocked(self) -> str:
        """Return why Add client is unavailable, or an empty string if available."""
        data = self._selected_data()
        if not data or data[0] != "iface":
            return _("zuerst ein Interface auswählen")
        if data[1] in self.busy:
            return _("wird gerade gelesen")
        if not any(i.name == data[2] for i in self.db.interfaces(data[1])):
            return _("zuerst „Vom Server übernehmen…“")
        return ""

    def _selected_row(self) -> PeerRow | None:
        selected = self._selected_key()
        return next((r for r in self.rows if r.public_key == selected), None) if selected else None

    def _select_key(self, public_key: str | None) -> None:
        """Restore selection by PublicKey after a table rebuild, regardless of sorting."""
        if not public_key:
            return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, KEY_COLUMN)
            if item is not None and item.text() == public_key:
                self.table.selectRow(row)
                return

    def language_setting(self) -> str:
        return settings.get("language", "system")

    def choose_language(self, code: str) -> None:
        settings.set_value("language", code)
        name = _("Systemsprache") if code == "system" else i18n.LANGUAGES.get(code, code)
        self.bar.show_message("info", _("Sprache „{name}“ gilt ab dem nächsten Start der App.").format(name=name))

    def set_auto_refresh(self, on: bool) -> None:
        settings.set_value("auto_refresh", on)
        if on:
            self.auto_timer.start()
        else:
            self.auto_timer.stop()

    def _auto_refresh(self) -> None:
        """Refresh all hosts only when idle and no dialog is open; reads only."""
        if self.busy or QApplication.activeModalWidget() is not None or not self.db.hosts():
            return
        log.debug("automatisch aktualisieren")
        self.refresh_all()

    def _apply_columns(self, *_args) -> None:
        """Use a compact default table; other columns are available through View."""
        for name in ("Empfangen", "Gesendet"):
            self.table.setColumnHidden(PEER_COLUMNS.index(name), not self.act_col_traffic.isChecked())
        self.table.setColumnHidden(KEY_COLUMN, not self.act_col_key.isChecked())

    def _focus_filter(self) -> None:
        if self.filter.isVisible():
            self.filter.setFocus()
            self.filter.selectAll()

    def _apply_filter(self, *_args) -> None:
        needle = self.filter.text().strip().lower()
        for row in range(self.table.rowCount()):
            text = " ".join((self.table.item(row, c).text() if self.table.item(row, c) else "")
                            for c in range(self.table.columnCount())).lower()
            self.table.setRowHidden(row, bool(needle) and needle not in text)

    def _selected_key(self) -> str | None:
        rows = {i.row() for i in self.table.selectedItems()}
        if len(rows) != 1 or self.table.isHidden():
            return None
        item = self.table.item(rows.pop(), KEY_COLUMN)
        return item.text() if item else None

    def select(self, host_id: int, interface: str | None = None) -> None:
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, ROLE)[1] != host_id:
                continue
            target = top
            for j in range(top.childCount()):
                if top.child(j).data(0, ROLE)[2] == interface:
                    target = top.child(j)
            self.tree.setCurrentItem(target)
            return

    def _host_icon(self, host_id: int) -> QIcon:
        if host_id in self.busy:
            return QIcon.fromTheme("view-refresh")
        if host_id in self.errors:
            return QIcon.fromTheme("dialog-error")
        return QIcon.fromTheme("network-server")

    def _host_label(self, host: Host) -> str:
        if host.id in self.busy:
            return _("{host_name} – wird gelesen").format(host_name=host.name)
        if host.id in self.errors:
            return _("{host_name} – Fehler").format(host_name=host.name)
        return host.name

    def _host_tip(self, host: Host) -> str:
        if host.id in self.busy:
            return _("{host_ssh_target} – wird gelesen").format(host_ssh_target=host.ssh_target)
        if host.id in self.errors:
            return _("{host_ssh_target} – Fehler beim letzten Lesen").format(host_ssh_target=host.ssh_target)
        return host.ssh_target

    def reload_tree(self) -> None:
        previous = self._selected_data()
        self.tree.clear()
        for host in self.db.hosts():
            state = self.states.get(host.id)
            top = QTreeWidgetItem([self._host_label(host)])
            top.setData(0, ROLE, ("host", host.id, None))
            top.setIcon(0, self._host_icon(host.id))
            top.setToolTip(0, self._host_tip(host))
            known = {i.name for i in self.db.interfaces(host.id)}
            names = sorted(known | set(state.interface_names if state else []))
            for name in names:
                imported = name in known
                child = QTreeWidgetItem([name if imported else f"{name} ({_(NOT_IMPORTED)})"])
                child.setData(0, ROLE, ("iface", host.id, name))
                child.setIcon(0, QIcon.fromTheme("network-vpn"))
                if not imported:
                    font = QFont(child.font(0))
                    font.setItalic(True)
                    child.setFont(0, font)
                    child.setToolTip(0, _("Steht nur auf dem Server. „Vom Server übernehmen…“ holt es in die App."))
                top.addChild(child)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        if previous:
            self.select(previous[1], previous[2])
        self.show_details()

    def _refresh_host_items(self) -> None:
        """Update host icons and tooltips for loading state without rebuilding the tree."""
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            host_id = top.data(0, ROLE)[1]
            top.setIcon(0, self._host_icon(host_id))
            with contextlib.suppress(KeyError):  # Host was just removed
                host = self.db.host(host_id)
                top.setToolTip(0, self._host_tip(host))
                top.setText(0, self._host_label(host))

    # --- Display -------------------------------------------------------------------------------

    def _table_selection_changed(self) -> None:
        if not self._rebuilding:
            self._peer_key = self._selected_key()
        self._update_actions()

    def show_details(self) -> None:
        self._rebuilding = True
        try:
            self._show_details()
        finally:
            self._rebuilding = False
        self._update_actions()

    def _show_details(self) -> None:
        self._update_actions()
        data = self._selected_data()
        self.table.setRowCount(0)
        self.table.hide()
        self.filter.hide()
        self.bar.clear()
        self.hints.clear()
        self.drift_bar.clear()
        has_hosts = bool(self.db.hosts())
        self.empty.setVisible(not has_hosts)
        self.info.setVisible(has_hosts)
        if not data:
            self.info.setText(_("Links einen Host oder ein Interface auswählen."))
            return
        kind, host_id, iface = data
        host = self.db.host(host_id)
        state = self.states.get(host_id)
        if host_id in self.errors:
            self.bar.show_message("error", f"{host.name}: {short_error(self.errors[host_id])}",
                                  (_("Protokoll anzeigen"), self.log_dock.show), detail=self.errors[host_id])
        elif host_id in self.flow_errors:
            self.bar.show_message("error", f"{host.name} – {self.flow_errors[host_id]}",
                                  (_("Protokoll anzeigen"), self.log_dock.show))
        if kind == "host":
            self._show_host(host, state)
        else:
            self._show_interface(host, iface, state)
            self._select_key(self._peer_key)

    def _read_line(self, host: Host, state: HostState | None) -> str:
        if host.id in self.busy:
            return _("wird gerade gelesen …")
        if state is None or host.id not in self.read_at:
            return _("noch nicht gelesen – <b>Aktualisieren</b> (F5)")
        return _("gelesen um {v1:%H:%M:%S}").format(v1=self.read_at[host.id])

    def _header(self, title: str, status: str, tone_kind: str, fields: list[tuple[str, str]]) -> str:
        """Build a rich-text header with title, status dot and text, and a field table."""
        dot = f'<span style="color:{tone(self, tone_kind).name()}">●</span> ' if tone_kind else ""
        rows = "".join(f'<tr><td style="color:{muted(self)}; padding-right:14px">{html.escape(k)}</td>'
                       f"<td>{v}</td></tr>" for k, v in fields)
        return (f'<span style="font-size:large; font-weight:600">{html.escape(title)}</span><br>'
                f"{dot}{status}<table cellspacing=0 cellpadding=1 style='margin-top:6px'>{rows}</table>")

    def _show_host(self, host: Host, state: HostState | None) -> None:
        check = self.checks.get(host.id)
        port = _(", Port {host_port}").format(host_port=host.port) if host.port else ""
        fields = [("SSH", f"<code>{html.escape(host.ssh_target)}</code>{port}"),
                  (_("System"), html.escape(host.os or _("noch nicht geprüft"))),
                  (_("Zugriff"), _("mit sudo") if host.sudo else _("direkt als root")),
                  (_("SSH-Schlüssel"), html.escape(host.identity_file) if host.identity_file
                   else _("System (SSH-Agent, ssh-Config)"))]
        if check and host.id not in self.errors:
            fields.append((_("Verbindung"), _("ok (getestet um {v1:%H:%M:%S})").format(v1=check[1])))
        if state is not None:
            units = ", ".join(state.active_units) or "keine"
            if not state.info.systemd:
                units = _("– (kein systemd)")
            fields.append((_("Interfaces"), _("{n_interface_names} · aktive Units: {v2}")
                .format(n_interface_names=len(state.interface_names), v2=html.escape(units))))
        if host.id in self.busy:
            status, kind = _("Wird gelesen …"), "info"
        elif host.id in self.errors:
            status, kind = _("Fehler beim letzten Lesen"), "error"
        elif state is not None:
            status, kind = _("Erreichbar"), "positive"
        else:
            status, kind = _("Noch nicht gelesen"), ""
        self.info.setText(self._header(host.name, f"{status} · {self._read_line(host, state)}", kind, fields))
        warnings = list(state.warnings) if state else (connection_problems(check[0]) if check else [])
        self._show_hints(warnings)

    def _show_hints(self, warnings: list[str]) -> None:
        if warnings:
            self.hints.show_message("warning", "\n".join(f"• {w}" for w in warnings))

    def _show_interface(self, host: Host, name: str, state: HostState | None) -> None:
        stored = next((i for i in self.db.interfaces(host.id) if i.name == name), None)
        conf = state.confs.get(name) if state else None
        runtime = state.runtime.get(name) if state else None

        rows: list[PeerRow] = []
        if stored is not None:
            rows = [PeerRow(p.name, p.public_key, p.allowed_ips, p.endpoint, None, disabled=p.disabled)
                    for p in self.db.peers(stored.id)]
            addresses, port, pubkey = stored.addresses, stored.listen_port, stored.public_key
            source = _("in der App")
        elif conf is not None:
            rows = [PeerRow(p.name, p.public_key, p.allowed_ips, p.endpoint, None, disabled=p.disabled)
                    for p in conf.peers]
            addresses, port = conf.addresses, conf.listen_port
            pubkey = state.public_keys.get(name) if state else None
            source = _("nur auf dem Server – mit „Vom Server übernehmen…“ in die App holen")
        else:
            addresses, port, pubkey = [], runtime.listen_port if runtime else None, None
            source = _("nur Laufzeit, keine Config-Datei")
        if runtime is not None:
            pubkey = pubkey or runtime.public_key
            known = {r.public_key for r in rows}
            for r in rows:
                r.runtime = runtime.peers.get(r.public_key)
            rows += [PeerRow(None, k, p.allowed_ips, p.endpoint, p, _("nur Laufzeit"))
                     for k, p in runtime.peers.items() if k not in known]

        if state is None:
            status, kind = _("Status unbekannt"), ""
        elif runtime is not None:
            active = sum(1 for r in rows if r.runtime and fmt.is_active(r.runtime.latest_handshake))
            status = _("Läuft · {active} von {total} Peers aktiv").format(active=active, total=len(rows))
            kind = "positive"
        else:
            status, kind = _("Läuft nicht"), "warning"
        drift: dict[str, str] = {}
        if stored is not None and conf is not None:
            drifts = sync.compute(self.db.peers(stored.id), conf, runtime)
            drift = {d.key: d.detail for d in drifts if d.key}
            if drifts:
                self.drift_bar.show_message(
                    "warning", ngettext("{n} Abweichung zwischen App, Server-Datei und Laufzeit.",
                                        "{n} Abweichungen zwischen App, Server-Datei und Laufzeit.",
                                        len(drifts)).format(n=len(drifts)),
                    (_("Abgleichen…"), self.sync_interface), detail="\n".join(f"{d.label}: {d.detail}" for d in drifts))
            else:
                status += _(" · App und Server stimmen überein")
        fields = [(_("Adresse"), html.escape(", ".join(addresses) or "–")), ("ListenPort", str(port or "–")),
                  (_("Quelle"), html.escape(source)),
                  ("PublicKey", f"<code>{html.escape(fmt.short_key(pubkey))}</code>"
                                + (f' &nbsp;<a href="copy-pubkey">{_("kopieren")}</a>' if pubkey else ""))]
        self._iface_pubkey = pubkey
        self.info.setText(self._header(f"{host.name} / {name}", f"{status} · {self._read_line(host, state)}",
                                       kind, fields))
        self._fill_table(rows, running=runtime is not None, drift=drift)
        self.table.show()
        self.filter.show()
        hints = list(state.warnings) if state else []
        if conf is not None and conf.save_config:
            hints.append(_("SaveConfig = true: wg-quick überschreibt die Datei beim Stoppen mit dem Laufzeitstand – "
                         "Kommentare, Namen und gesperrte Peers gehen dabei verloren. Empfehlung: Zeile entfernen."))
        self._show_hints(hints)

    def _fill_table(self, rows: list[PeerRow], running: bool, drift: dict[str, str] | None = None) -> None:
        drift = drift or {}
        self.rows = rows
        self.table.setSortingEnabled(False)  # Disable while filling to prevent row moves (Qt QTableWidget docs)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            rt = r.runtime
            name = r.name or _("Peer {v1}").format(v1=fmt.short_key(r.public_key))
            if r.note:
                name += f" ({r.note})"
            status, icon, tip = peer_status(rt, running, r.disabled)
            values = [
                name,
                status,
                ", ".join(r.allowed_ips),
                (rt.endpoint if rt and rt.endpoint else r.endpoint) or "–",
                fmt.handshake_h(rt.latest_handshake) if rt else "–",
                fmt.bytes_h(rt.rx_bytes) if rt else "–",
                fmt.bytes_h(rt.tx_bytes) if rt else "–",
                r.public_key,
            ]
            ranks = {4: rt.latest_handshake if rt else -1, 5: rt.rx_bytes if rt else -1,
                     6: rt.tx_bytes if rt else -1}
            for col, text in enumerate(values):
                item = _SortItem(text, ranks.get(col))
                if col == 0 and r.public_key in drift:
                    item.setIcon(theme_icon(self, "warning"))  # Qt fallback if no icon theme is available
                    item.setToolTip(_("Weicht vom Server ab: {v1} – „Mit Server abgleichen…“")
                        .format(v1=drift[r.public_key]))
                if col == 1:
                    item.setToolTip(tip)
                    if icon:
                        item.setIcon(QIcon.fromTheme(icon))
                self.table.setItem(i, col, item)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        for col in range(len(PEER_COLUMNS) - 1):  # Keep long AllowedIPs/endpoints from crowding status columns
            self.table.setColumnWidth(col, min(self.table.columnWidth(col), 260))
        self._apply_filter()

    def _info_link(self, link: str) -> None:
        if link == "copy-pubkey" and self._iface_pubkey:
            QGuiApplication.clipboard().setText(self._iface_pubkey)
            self.statusBar().showMessage(_("PublicKey des Interfaces kopiert"), 3000)

    def copy_public_key(self) -> None:
        key = self._selected_key()
        if key:
            QGuiApplication.clipboard().setText(key)
            self.statusBar().showMessage(_("PublicKey kopiert"), 3000)

    # --- Hosts ---------------------------------------------------------------------------------

    def _tree_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, ROLE)[0] == "host":
            self.edit_host()

    def add_host(self) -> None:
        values = self.ask_host(None)
        if values is None:
            return
        try:
            host_id = self.db.add_host(*values)
        except Exception as err:  # noqa: BLE001 – e.g. duplicate name
            log.warning("Host hinzufügen fehlgeschlagen: %s", err)
            self.error("Host hinzufügen", f"{err}\n\nAnderen Namen wählen.")
            return
        log.info("Host hinzugefügt: %s", values[0])
        self.reload_tree()
        self.select(host_id)
        self.refresh_host(self.db.host(host_id))  # Read immediately; show status without another click

    def show_host_key(self) -> None:
        host = self.selected_host()
        if host is None or not host.identity_file:
            return
        text = sshkeys.public_key_text(host.identity_file)
        if text is None:
            self._flow_error(host, "SSH-Public-Key", _("{host_identity_file}.pub nicht gefunden oder nicht lesbar.")
                .format(host_identity_file=host.identity_file))
            return
        self.show_public_key(host, text)

    def show_public_key(self, host: Host, text: str) -> None:
        KeyDialog(host.name, host.ssh_target, host.identity_file or "", text, self).exec()

    def edit_host(self) -> None:
        host = self.selected_host()
        if host is None or host.id in self.busy:
            return
        values = self.ask_host(host)
        if values is None:
            return
        try:
            self.db.update_host(host.id, *values)
        except Exception as err:  # noqa: BLE001
            log.warning("Host bearbeiten fehlgeschlagen: %s", err)
            self.error("Host bearbeiten", f"{err}\n\nAnderen Namen wählen.")
            return
        log.info("Host geändert: %s", values[0])
        for store in (self.states, self.errors, self.read_at, self.checks):
            store.pop(host.id, None)
        self.reload_tree()
        self.refresh_host(self.db.host(host.id))

    def delete_host(self) -> None:
        host = self.selected_host()
        if host is None or host.id in self.busy:
            return
        if not self.confirm_delete(host):
            return
        self.db.delete_host(host.id)
        log.info("Host entfernt: %s", host.name)
        for store in (self.states, self.errors, self.read_at, self.checks):
            store.pop(host.id, None)
        self.reload_tree()
        self.statusBar().showMessage(f"{host.name} entfernt", 5000)

    # --- SSH (background) ----------------------------------------------------------------------

    def _ssh(self, host: Host, fn: Callable[[Runner], Any], done: Callable[[Any], None], verb: str = "") -> None:
        """Run fn(runner) in the background, one call per host at a time. While it runs,
        disable host actions and show progress in the host icon and status bar."""
        if host.id in self.busy:
            return
        runner = self.runner_factory(host)
        if not self.busy:
            self.round.clear()
        self.busy[host.id] = f"{verb or _('Lese')} {host.name}"
        self.flow_errors.pop(host.id, None)

        def finish(ok: bool) -> bool:
            self.busy.pop(host.id, None)
            self.round[host.name] = ok
            try:
                self.db.host(host.id)
            except KeyError:  # Removed in the meantime
                return False
            return True

        def on_done(result: Any) -> None:
            if finish(True):
                done(result)
            self._busy_changed()

        def on_error(message: str) -> None:
            if finish(False):
                self._ssh_failed(host, message)
            self._busy_changed()

        self._busy_changed()
        self.bg.start(lambda: fn(runner), on_done, on_error)

    def _busy_changed(self) -> None:
        self._refresh_host_items()
        self.show_details()  # Update action availability and the reading status
        self.progress.setVisible(bool(self.busy))
        if self.busy:
            self.statusBar().showMessage(f"{', '.join(sorted(self.busy.values()))} …")
            return
        failed = sorted(name for name, ok in self.round.items() if not ok)
        parts = [f"{name} {'ok' if ok else 'Fehler'}" for name, ok in sorted(self.round.items())]
        text = _("Fertig um {v1:%H:%M:%S}: ").format(v1=datetime.now()) + ", ".join(parts)
        if failed:
            text += _(" – Details im Protokoll (Strg+L)")
        if self.notice:  # Action result first, then the summary
            text = f"{self.notice} · {text}"
            self.notice = ""
        self.statusBar().showMessage(text, 10000)

    def _flow_error(self, host: Host, title: str, text: str) -> None:
        """Show a flow error in the host message bar (KDE HIG)."""
        log.warning("%s: %s: %s", host.name, title, text)
        self.flow_errors[host.id] = f"{title}: {text}"
        self.show_details()

    def _notify(self, text: str) -> None:
        """Show an action result in the status bar, even if SSH continues afterward (KDE HIG:
        feedback without a dialog)."""
        self.notice = text
        self.statusBar().showMessage(text, 10000)

    def test_connection(self) -> None:
        host = self.selected_host()
        if host is None:
            return
        log.info("%s: Verbindung testen", host.name)

        def done(info: ConnectionInfo) -> None:
            self.db.set_host_os(host.id, info.os_name)
            self.errors.pop(host.id, None)
            self.checks[host.id] = (info, datetime.now())
            self.reload_tree()

        self._ssh(host, check_connection, done)

    def refresh_all(self) -> None:
        for host in self.db.hosts():
            self.refresh_host(host)

    def refresh_host(self, host: Host, then: Callable[[HostState], None] | None = None) -> None:
        log.info("%s: lesen", host.name)

        def done(state: HostState) -> None:
            self.states[host.id] = state
            self.read_at[host.id] = datetime.now()
            self.errors.pop(host.id, None)
            self.db.set_host_os(host.id, state.info.os_name)
            self.reload_tree()
            if then:
                then(state)

        self._ssh(host, discover, done)

    def _ssh_failed(self, host: Host, message: str) -> None:
        log.error("%s: %s", host.name, message)
        self.errors[host.id] = message
        self.reload_tree()  # Log no longer auto-opens; the message has a Show log button

    def import_from_server(self) -> None:
        host = self.selected_host()
        if host is None:
            return
        # Always read fresh data so the preview reflects the current state
        self.refresh_host(host, then=lambda state: self._import(host, state))

    def _import(self, host: Host, state: HostState) -> None:
        changes = self.db.plan_import(host.id, state)
        if not changes:
            self._notify(_("{host_name}: App und Server stimmen überein").format(host_name=host.name))
            return
        if not self.confirm_import(host.name, changes):
            return
        self.db.apply_import(host.id, state)
        log.info("%s: %d Änderung(en) in die App übernommen", host.name, len(changes))
        self._notify(ngettext("{host}: {n} Änderung in die App übernommen",
                              "{host}: {n} Änderungen in die App übernommen", len(changes))
                     .format(host=host.name, n=len(changes)))
        self.reload_tree()

    # --- Add client (stage 2, writes to server) -------------------------------------------------

    def add_client(self) -> None:
        """Read fresh data and checksum → dialog → plan with diff → write and confirm → QR code."""
        data = self._selected_data()
        host = self.selected_host()
        if host is None or self._client_blocked() or not data:
            return
        iface = data[2]
        log.info("%s/%s: Client hinzufügen – lese aktuellen Stand", host.name, iface)

        def read(runner: Runner) -> tuple[HostState, apply.Prepared, list[str]]:
            return discover(runner), apply.prepare(runner, iface), runner.remote_addresses()

        def done(result: tuple[HostState, apply.Prepared, list[str]]) -> None:
            state, prepared, remote_ips = result
            self.states[host.id] = state
            self.read_at[host.id] = datetime.now()
            self.errors.pop(host.id, None)
            self.reload_tree()
            self._client_dialog(host, iface, state, prepared, remote_ips)

        self._ssh(host, read, done)

    def _client_dialog(self, host: Host, iface: str, state: HostState, prepared: apply.Prepared,
                       remote_ips: list[str]) -> None:
        stored = next((i for i in self.db.interfaces(host.id) if i.name == iface), None)
        conf = state.confs.get(iface)
        runtime = state.runtime.get(iface)
        server_key = state.public_keys.get(iface) or (runtime.public_key if runtime else None)
        if stored is None or conf is None or not server_key:
            self._flow_error(host, _("Client hinzufügen"),
                             _("{iface}: Config-Datei oder Public Key nicht lesbar. Host neu lesen (F5) und "
                               "Hinweise im Fenster prüfen.").format(iface=iface))
            return
        used = [ip for p in conf.peers for ip in p.allowed_ips]
        used += [ip for p in (runtime.peers.values() if runtime else []) for ip in p.allowed_ips]
        used += [ip for p in self.db.peers(stored.id) for ip in p.allowed_ips]
        suggested = client.suggest_addresses(conf.addresses, used)
        endpoint = stored.client_endpoint or client.default_endpoint(host.ssh_target, conf.listen_port)
        spec = self.ask_client(f"{host.name} / {iface}", conf.addresses, used, suggested, endpoint,
                               stored.client_dns)
        if spec is None:
            return

        pair = keys.generate_keypair()
        psk = keys.generate_psk() if spec.psk else None
        warnings = []
        if apply.ssh_over_interface(remote_ips, conf.addresses):
            warnings.append(_("Die SSH-Verbindung zu {host_name} läuft über {iface} – also über den eigenen Zugang. "
                              "Bricht sie ab, setzt {host_name} die Änderung nach {apply_ROLLBACK_SECONDS} s selbst "
                              "zurück.")
                                  .format(host_name=host.name,
                                          iface=iface,
                                          apply_ROLLBACK_SECONDS=apply.ROLLBACK_SECONDS))
        if conf.save_config:
            warnings.append(_("SaveConfig = true: wg-quick schreibt beim Stoppen den Laufzeitstand in die Datei. "
                            "Der neue Peer ist dort enthalten, Kommentare können verloren gehen."))
        plan = apply.WritePlan(
            host.name, iface, WG_DIR,
            "".join(f"+{line}\n" for line in
                    client.preview_peer_block(spec.name, pair.public, spec.addresses, psk is not None).splitlines()),
            client.server_peer_block(spec.name, pair.public, spec.addresses, psk),
            prepared.conf_hash, prepared.running, apply.make_tag(), warnings=tuple(warnings))
        try:
            apply.check_plan(plan, prepared)
        except ValueError as err:
            log.warning("%s: Schreiben nicht möglich: %s", host.name, err)
            self._flow_error(host, _("Client hinzufügen"), str(err))
            return
        if not self.confirm_write(plan):
            log.info("%s: Schreiben abgebrochen", host.name)
            return

        def written(mode: str) -> None:
            self.db.add_peer(stored.id, spec.name, pair.public, spec.addresses, psk is not None)
            self.db.set_client_defaults(stored.id, spec.endpoint, ", ".join(spec.dns))  # Store only after success
            log.info("%s/%s: Client %s angelegt (%s)", host.name, iface, spec.name, mode)
            self._notify(_("{host_name}: Client {spec_name} angelegt").format(host_name=host.name, spec_name=spec.name))
            text = client.client_config(spec, pair.private, server_key, psk, conf.addresses)
            self.refresh_host(host)
            self.show_client(spec.name, text)

        self._ssh(host, lambda runner: apply.write_and_confirm(runner, plan), written, verb=_("Schreibe auf"))

    # --- Edit peers and sync (stage 3, writes to server) ----------------------------------------

    def _store_state(self, host: Host, state: HostState) -> None:
        self.states[host.id] = state
        self.read_at[host.id] = datetime.now()
        self.errors.pop(host.id, None)
        self.db.set_host_os(host.id, state.info.os_name)
        self.reload_tree()

    def _peer_label(self, conf: InterfaceConf, key: str) -> str:
        peer = next((p for p in conf.peers if p.public_key == key), None)
        return (peer.name if peer and peer.name else None) or _("Peer {v1}").format(v1=fmt.short_key(key))

    def _change_server(self, title: str, build: Builder) -> None:
        """Shared flow: read fresh data and checksum → build (dialogs) → diff on the redacted copy →
        plan → write and confirm → update the app database."""
        data = self._selected_data()
        host = self.selected_host()
        if host is None or self._client_blocked() or not data:
            return
        iface = data[2]
        log.info("%s/%s: %s – lese aktuellen Stand", host.name, iface, title)

        def read(runner: Runner) -> tuple[HostState, apply.Prepared, list[str]]:
            return discover(runner), apply.prepare(runner, iface), runner.remote_addresses()

        def done(result: tuple[HostState, apply.Prepared, list[str]]) -> None:
            state, prepared, remote_ips = result
            self._store_state(host, state)
            self.select(host.id, iface)
            conf, text = state.confs.get(iface), state.conf_texts.get(iface)
            if conf is None or text is None:
                self._flow_error(host, title, _("{iface}: Config-Datei nicht lesbar. Hinweise im Fenster prüfen.")
                    .format(iface=iface))
                return
            built = build(host, iface, state, conf)
            if built is not None:
                self._write_ops(title, host, iface, conf, text, prepared, remote_ips, *built)

        self._ssh(host, read, done)

    def _write_ops(self, title: str, host: Host, iface: str, conf: InterfaceConf, text: str,
                   prepared: apply.Prepared, remote_ips: list[str], ops: list[edit.PeerOp], sync_runtime: bool,
                   action: str, warnings: list[str], after: Callable[[], None] | None) -> None:
        try:
            new_text = edit.transform_local(text, ops) if ops else text
        except ValueError as err:
            log.warning("%s: %s nicht möglich: %s", host.name, title, err)
            self._flow_error(host, title, str(err))
            return
        if ops and new_text == text:
            self._notify(_("{host_name}/{iface}: keine Änderung nötig").format(host_name=host.name, iface=iface))
            return
        preview = (edit.diff_lines(text, new_text) if ops else
                   _("(Die Datei bleibt unverändert – wg syncconf lädt sie neu in die Laufzeit.)\n"))
        warnings = list(warnings)
        risky = sync_runtime or any(o.op in ("remove", "state", "edit", "rekey") for o in ops)
        if risky and apply.ssh_over_interface(remote_ips, conf.addresses):
            warnings.insert(0, _("Die SSH-Verbindung zu {host_name} läuft über {iface}. Betrifft die Änderung den "
                                 "eigenen "
                                 "Zugang, bricht sie ab – dann setzt {host_name} nach {apply_ROLLBACK_SECONDS} s alles "
                                 "selbst zurück.")
                                     .format(host_name=host.name,
                                             iface=iface,
                                             apply_ROLLBACK_SECONDS=apply.ROLLBACK_SECONDS))
        if conf.save_config:
            warnings.append(_("SaveConfig = true: Beim Stoppen überschreibt wg-quick die Datei mit dem Laufzeitstand; "
                            "gesperrte Peers und Kommentare gehen dann verloren."))
        plan = apply.WritePlan(
            host.name, iface, WG_DIR, preview, edit.order_text(ops) if ops else "", prepared.conf_hash,
            prepared.running, apply.make_tag(), warnings=tuple(warnings),
            builder=apply.BUILD_EDIT if ops else apply.BUILD_COPY, builder_args=(edit.EDIT_AWK,) if ops else (),
            action=action)
        try:
            apply.check_plan(plan, prepared)
        except ValueError as err:
            log.warning("%s: Schreiben nicht möglich: %s", host.name, err)
            self._flow_error(host, title, str(err))
            return
        if not self.confirm_write(plan):
            log.info("%s: %s abgebrochen", host.name, title)
            return
        new_peers = {p.public_key: p for p in parse_conf(new_text).peers}

        def written(mode: str) -> None:
            stored = next((i for i in self.db.interfaces(host.id) if i.name == iface), None)
            if stored is not None:
                for o in ops:
                    if o.op in ("remove", "rekey"):
                        self.db.delete_peer(stored.id, o.public_key)
                    if o.op != "remove":
                        self.db.upsert_peer(stored.id, new_peers[o.new_key if o.op == "rekey" else o.public_key])
            log.info("%s/%s: %s (%s)", host.name, iface, action, mode)
            self._notify(_("{host_name}/{iface}: {action} – erledigt")
                .format(host_name=host.name, iface=iface, action=action))
            self.refresh_host(host)
            if after is not None:
                after()

        self._ssh(host, lambda runner: apply.write_and_confirm(runner, plan), written, verb=_("Schreibe auf"))

    def _conf_peer(self, host: Host, title: str, conf: InterfaceConf, key: str | None) -> PeerConf | None:
        peer = next((p for p in conf.peers if p.public_key == key), None)
        if peer is None:
            self._flow_error(host, title, _("Der Peer steht nicht (mehr) in der Server-Datei. „Mit Server "
                                          "abgleichen…“ zeigt, was abweicht."))
        return peer

    def edit_peer(self) -> None:
        key = self._selected_key()

        def build(host: Host, iface: str, state: HostState, conf: InterfaceConf):
            peer = self._conf_peer(host, _("Peer bearbeiten"), conf, key)
            if peer is None:
                return None
            others = {self._peer_label(conf, p.public_key): p.allowed_ips for p in conf.peers
                      if p.public_key != key and not p.disabled}
            op = self.ask_peer(f"{host.name} / {iface}", peer, others)
            if op is None:
                return None
            return [op], False, _("{v1} ändern").format(v1=self._peer_label(conf, peer.public_key)), [], None

        self._change_server(_("Peer bearbeiten"), build)

    def toggle_peer(self) -> None:
        key = self._selected_key()

        def build(host: Host, iface: str, state: HostState, conf: InterfaceConf):
            peer = self._conf_peer(host, _("Peer sperren"), conf, key)
            if peer is None:
                return None
            label = self._peer_label(conf, peer.public_key)
            if peer.disabled:
                return [edit.PeerOp("state", peer.public_key, state="on")], False, f"{label} entsperren", [], None
            return ([edit.PeerOp("state", peer.public_key, state="off")], False, f"{label} sperren",
                    [_("{label} kann sich danach nicht mehr verbinden. Der Block bleibt auskommentiert in der "
                       "Datei; „Peer entsperren…“ stellt ihn wieder her.").format(label=label)], None)

        self._change_server(_("Peer sperren"), build)

    def remove_peer(self) -> None:
        key = self._selected_key()

        def build(host: Host, iface: str, state: HostState, conf: InterfaceConf):
            peer = self._conf_peer(host, _("Peer entfernen"), conf, key)
            if peer is None:
                return None
            label = self._peer_label(conf, peer.public_key)
            return ([edit.PeerOp("remove", peer.public_key)], False, f"{label} entfernen",
                    [_("{label} wird aus der Datei gelöscht (das Backup bleibt auf dem Server). Der Client kann "
                       "sich nicht mehr verbinden; seine Schlüssel lassen sich nicht wiederherstellen. Nur "
                       "vorübergehend? Dann „Peer sperren…“.").format(label=label)], None)

        self._change_server(_("Peer entfernen"), build)

    def rekey_peer(self) -> None:
        """Issue a new QR code for an existing client with new keys and PSK; keep name and address.
        The app does not know the old private key, so it must reissue rather than display it."""
        selected = self._selected_key()

        def build(host: Host, iface: str, state: HostState, conf: InterfaceConf):
            peer = self._conf_peer(host, _("Neuen QR-Code ausstellen"), conf, selected)
            if peer is None:
                return None
            label = self._peer_label(conf, peer.public_key)
            nets = client.interface_networks(conf.addresses)
            own = [a for a in peer.allowed_ips if client.is_host_in(a, nets)]
            if not own:
                self._flow_error(host, _("Neuen QR-Code ausstellen"),
                                 _("{label} hat keine einzelne Adresse im VPN-Netz (z. B. Standort-Tunnel) – "
                                   "dafür gibt es keine Client-Config.").format(label=label))
                return None
            runtime = state.runtime.get(iface)
            server_key = state.public_keys.get(iface) or (runtime.public_key if runtime else None)
            stored = next((i for i in self.db.interfaces(host.id) if i.name == iface), None)
            if not server_key or stored is None:
                self._flow_error(host, _("Neuen QR-Code ausstellen"), _("Public Key des Servers nicht lesbar."))
                return None
            others = [ip for p in conf.peers if p.public_key != peer.public_key for ip in p.allowed_ips]
            endpoint = stored.client_endpoint or client.default_endpoint(host.ssh_target, conf.listen_port)
            spec = self.ask_client(f"{host.name} / {iface}", conf.addresses, others, own, endpoint,
                                   stored.client_dns, fixed_name=label)
            if spec is None:
                return None
            pair = keys.generate_keypair()
            psk = keys.generate_psk() if spec.psk else None
            op = edit.PeerOp("rekey", peer.public_key, new_key=pair.public, psk=psk or "")
            warnings = [_("{label} bekommt neue Schlüssel. Das bisherige Gerät verbindet sich erst wieder, wenn es "
                          "den neuen QR-Code gescannt hat.").format(label=label)]

            def show() -> None:
                self.db.set_client_defaults(stored.id, spec.endpoint, ", ".join(spec.dns))
                self.show_client(label, client.client_config(spec, pair.private, server_key, psk, conf.addresses))

            return [op], False, _("{label}: neue Schlüssel ausstellen").format(label=label), warnings, show

        self._change_server(_("Neuen QR-Code ausstellen"), build)

    # --- Stage 4: install, new interface, server-to-server tunnel ---------------------------------------

    def install_tools(self) -> None:
        host = self.selected_host()
        if host is None or host.id in self.busy:
            return

        def checked(info: ConnectionInfo) -> None:
            if info.wg_installed:
                self._notify(_("{host}: wireguard-tools sind schon installiert").format(host=host.name))
                return
            argv = provision.install_command(info.family)
            if argv is None:
                self._flow_error(host, _("wireguard-tools installieren"),
                                 _("Keine automatische Installation für diese Distribution. Von Hand: {hint}")
                                 .format(hint=info.install_hint))
                return
            if not self.confirm_install(host, " ".join(argv)):
                return

            def installed(_result: object) -> None:
                self._notify(_("{host}: wireguard-tools installiert").format(host=host.name))
                self.refresh_host(host)

            self._ssh(host, lambda runner: provision.install_tools(runner, info.family), installed,
                      verb=_("Installiere auf"))

        self._ssh(host, check_connection, checked)

    def _existing_interfaces(self, state: HostState) -> dict[str, tuple[list[str], int | None]]:
        out: dict[str, tuple[list[str], int | None]] = {}
        for name, conf in state.confs.items():
            out[name] = (conf.addresses, conf.listen_port)
        for name, rt in state.runtime.items():
            out.setdefault(name, ([], rt.listen_port))
        return out

    def new_interface(self) -> None:
        host = self.selected_host()
        if host is None or host.id in self.busy:
            return

        def read(state: HostState) -> None:
            self._store_state(host, state)
            if not state.info.wg_installed:
                self._flow_error(host, _("Neues Interface"), _("wireguard-tools fehlen – zuerst „wireguard-tools "
                                                               "installieren…“."))
                return
            new = self.ask_new_interface(host, self._existing_interfaces(state), state.info.systemd)
            if new is None or not self.confirm_write(new):
                return

            def created(public_key: str) -> None:
                self._notify(_("{host}: Interface {name} angelegt – „Vom Server übernehmen…“ holt es in die App")
                             .format(host=host.name, name=new.name))
                log.info("%s: Interface %s angelegt, PublicKey %s…", host.name, new.name, public_key[:8])
                self.refresh_host(host)

            def create(runner: Runner) -> str:
                key = provision.create_interface(runner, new)
                provision.confirm_interface(runner, new)
                return key

            self._ssh(host, create, created, verb=_("Schreibe auf"))

        self._ssh(host, discover, read)

    def _tunnel_ends(self) -> list[TunnelEnd]:
        ends = []
        for h in self.db.hosts():
            for iface in self.db.interfaces(h.id):
                if not iface.public_key:
                    continue
                endpoint = iface.client_endpoint or client.default_endpoint(h.ssh_target, iface.listen_port)
                ends.append(TunnelEnd(h.id, h.name, iface.name, iface.public_key, tuple(iface.addresses), endpoint))
        return ends

    def new_tunnel(self) -> None:
        data = self._selected_data()
        host_a = self.selected_host()
        if host_a is None or self._client_blocked() or not data:
            return
        ends = self._tunnel_ends()
        own = next((e for e in ends if e.host_id == host_a.id and e.iface == data[2]), None)
        if own is None:
            self._flow_error(host_a, _("Server-Tunnel anlegen"), _("Public Key dieses Interfaces unbekannt – zuerst "
                                                                     "„Vom Server übernehmen…“."))
            return
        others = [e for e in ends if (e.host_id, e.iface) != (own.host_id, own.iface)]
        choice = self.ask_tunnel(own, others)
        if choice is None:
            return
        side_a, side_b, end_b, use_psk, keepalive = choice
        host_b = self.db.host(end_b.host_id)
        psk = keys.generate_psk() if use_psk else None

        def read_a(prep_a: apply.Prepared) -> None:
            def read_b(prep_b: apply.Prepared) -> None:
                plan_a, plan_b = provision.tunnel_plans(side_a, side_b, psk, (prep_a.conf_hash, prep_b.conf_hash),
                                                        (prep_a.running, prep_b.running), keepalive)
                for plan, prep, host in ((plan_a, prep_a, host_a), (plan_b, prep_b, host_b)):
                    try:
                        apply.check_plan(plan, prep)
                    except ValueError as err:
                        self._flow_error(host, _("Server-Tunnel anlegen"), str(err))
                        return
                if not self.confirm_write(plan_a):
                    return

                def written_a(_mode: str) -> None:
                    if not self.confirm_write(plan_b):
                        self._flow_error(host_a, _("Server-Tunnel anlegen"),
                                         _("Nur Seite A geschrieben – Seite B abgebrochen. Den Peer auf A ggf. "
                                           "wieder entfernen."))
                        self.refresh_host(host_a)
                        return

                    def written_b(_mode: str) -> None:
                        self._notify(_("Tunnel {a} ↔ {b} angelegt").format(a=own.label, b=end_b.label))
                        self.refresh_host(host_a)
                        self.refresh_host(host_b)

                    self._ssh(host_b, lambda r: apply.write_and_confirm(r, plan_b), written_b, verb=_("Schreibe auf"))

                self._ssh(host_a, lambda r: apply.write_and_confirm(r, plan_a), written_a, verb=_("Schreibe auf"))

            self._ssh(host_b, lambda r: apply.prepare(r, end_b.iface), read_b)

        self._ssh(host_a, lambda r: apply.prepare(r, own.iface), read_a)

    def prune_backups(self) -> None:
        """Delete old interface backups (*.wggui-<zeit>): list, confirm filenames, then delete."""
        data = self._selected_data()
        host = self.selected_host()
        if host is None or self._client_blocked() or not data:
            return
        iface = data[2]

        def listed(tags: list[str]) -> None:
            old = apply.backups_to_prune(tags)
            if not old:
                self._notify(_("{host}/{iface}: keine alten Backups (die neuesten {n} bleiben)")
                             .format(host=host.name, iface=iface, n=apply.KEEP_BACKUPS))
                return
            files = [f"{WG_DIR}/{iface}.conf.wggui-{t}" for t in old]
            if not self.confirm_prune(host, files, len(tags) - len(old)):
                return

            def pruned(removed: list[str]) -> None:
                self._notify(ngettext("{host}/{iface}: {n} Backup gelöscht", "{host}/{iface}: {n} Backups gelöscht",
                                      len(removed)).format(host=host.name, iface=iface, n=len(removed)))

            self._ssh(host, lambda runner: apply.prune_backups(runner, iface, old), pruned, verb=_("Lösche auf"))

        self._ssh(host, lambda runner: apply.list_backups(runner, iface), listed)

    def sync_interface(self) -> None:
        def build(host: Host, iface: str, state: HostState, conf: InterfaceConf):
            stored = next((i for i in self.db.interfaces(host.id) if i.name == iface), None)
            if stored is None:
                return None
            app_peers = self.db.peers(stored.id)
            drifts = sync.compute(app_peers, conf, state.runtime.get(iface))
            if not drifts:
                self._notify(_("{host_name}/{iface}: App, Datei und Laufzeit stimmen überein")
                    .format(host_name=host.name, iface=iface))
                return None
            choice = self.ask_sync(f"{host.name} / {iface}", drifts)
            if choice is None:
                return None
            res = sync.resolve(drifts, choice, app_peers, conf)

            def apply_db() -> None:
                for peer in res.db_upserts:
                    self.db.upsert_peer(stored.id, peer)
                for key in res.db_deletes:
                    self.db.delete_peer(stored.id, key)
                if res.db_upserts or res.db_deletes:
                    n = len(res.db_upserts) + len(res.db_deletes)
                    log.info("%s/%s: Abgleich – %d Änderung(en) in der App", host.name, iface, n)
                    self._notify(ngettext("{host}/{iface}: {n} Änderung in die App übernommen",
                                          "{host}/{iface}: {n} Änderungen in die App übernommen", n)
                                 .format(host=host.name, iface=iface, n=n))
                    self.reload_tree()

            if not res.writes_server:
                apply_db()  # App only: apply immediately
                return None
            action = (ngettext("{n} Peer-Änderung aus dem Abgleich", "{n} Peer-Änderungen aus dem Abgleich",
                               len(res.ops)).format(n=len(res.ops)) if res.ops
                      else _("Laufzeit an die Datei angleichen"))
            return res.ops, res.sync_runtime, action, [], apply_db  # Update app only after a successful write

        self._change_server(_("Mit Server abgleichen"), build)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 – Qt name
        self.log_dock.detach()
        super().closeEvent(event)

    # --- Prompts and messages (overridable in tests) -------------------------------------------

    def ask_host(self, host: Host | None) -> tuple[str, str, int | None, bool, str | None] | None:
        dlg = HostDialog(self, host, {h.name for h in self.db.hosts()})
        return dlg.values() if dlg.exec() == HostDialog.DialogCode.Accepted else None

    def confirm_install(self, host: Host, command: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, _("wireguard-tools installieren"),
                          _("Auf {host} ausführen?\n\n{command}").format(host=host.name, command=command), parent=self)
        run = box.addButton(_("Installieren"), QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(box.addButton(QMessageBox.StandardButton.Cancel))
        box.exec()
        return box.clickedButton() is run

    def ask_new_interface(self, host: Host, existing: dict, systemd: bool) -> provision.NewInterface | None:
        dlg = NewInterfaceDialog(host.name, existing, systemd, self)
        return dlg.value() if dlg.exec() == NewInterfaceDialog.DialogCode.Accepted else None

    def ask_tunnel(self, own: TunnelEnd, others: list[TunnelEnd]):  # noqa: ANN201 – tuple from TunnelDialog
        dlg = TunnelDialog(own, others, self)
        return dlg.value() if dlg.exec() == TunnelDialog.DialogCode.Accepted else None

    def confirm_prune(self, host: Host, files: list[str], kept: int) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, _("Alte Backups aufräumen"),
                          ngettext("Auf {host} {n} alte Sicherung löschen?", "Auf {host} {n} alte Sicherungen löschen?",
                                   len(files)).format(host=host.name, n=len(files))
                          + "\n\n" + "\n".join(files) + "\n\n"
                          + _("Es bleiben die neuesten {kept}. Die Config selbst ändert sich nicht.").format(kept=kept),
                          parent=self)
        remove = box.addButton(_("Löschen"), QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(box.addButton(QMessageBox.StandardButton.Cancel))
        box.exec()
        return box.clickedButton() is remove

    def confirm_delete(self, host: Host) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, _("Host entfernen"),
                          _("{host_name} mit allen Interfaces und Peers aus der App entfernen?\n\nAuf dem Server wird "
                            "nichts geändert.").format(host_name=host.name), parent=self)
        remove = box.addButton(_("Entfernen"), QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(box.addButton(QMessageBox.StandardButton.Cancel))
        box.exec()
        return box.clickedButton() is remove

    def ask_client(self, title: str, interface_addresses: list[str], used: list[str], suggested: list[str],
                   endpoint: str, dns: str, fixed_name: str | None = None) -> client.ClientSpec | None:
        dlg = ClientDialog(title, interface_addresses, used, suggested, endpoint, dns, self, fixed_name)
        return dlg.spec() if dlg.exec() == ClientDialog.DialogCode.Accepted else None

    def confirm_write(self, plan: apply.WritePlan) -> bool:
        return PlanDialog(plan, self).exec() == PlanDialog.DialogCode.Accepted

    def ask_peer(self, title: str, peer: PeerConf, others: dict[str, list[str]]) -> edit.PeerOp | None:
        dlg = PeerDialog(title, peer, others, self)
        return dlg.op() if dlg.exec() == PeerDialog.DialogCode.Accepted else None

    def ask_sync(self, title: str, drifts: list[sync.Drift]) -> dict[int, str] | None:
        dlg = SyncDialog(title, drifts, self)
        return dlg.choices() if dlg.exec() == SyncDialog.DialogCode.Accepted else None

    def show_client(self, name: str, config: str) -> None:
        QrDialog(name, config, self).exec()

    def _confirm_import_dialog(self, host_name: str, changes: list[Change]) -> bool:
        return ImportDialog(host_name, changes, self).exec() == ImportDialog.DialogCode.Accepted

    def message(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def error(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)
