"""GUI tests with pytest-qt (offscreen). FakeRunner replaces SSH."""

import pytest
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QLabel, QToolBar, QTreeWidget

from tests.conftest import debian_runner
from tests.keys import K1, K9
from wggui.db import Database
from wggui.fmt import short_key
from wggui.remote import FakeRunner
from wggui.ui.dialogs import HostDialog, ImportDialog
from wggui.ui.main_window import PEER_COLUMNS, MainWindow


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "gui.db")
    yield d
    d.close()


@pytest.fixture
def window(qtbot, db):
    confirmed = []

    def confirm(host_name, changes):
        confirmed.append((host_name, changes))
        return True

    w = MainWindow(db, runner_factory=lambda host: debian_runner(), confirm_import=confirm)
    w.confirmed = confirmed
    w.messages = []
    w.message = lambda title, text: w.messages.append((title, text))
    w.error = lambda title, text: w.messages.append((title, text))
    qtbot.addWidget(w)
    return w


def _wait(qtbot, w):
    qtbot.waitUntil(lambda: not w.bg.busy, timeout=5000)


def _column(w, name):
    col = PEER_COLUMNS.index(name)
    return [w.table.item(r, col).text() for r in range(w.table.rowCount())]


def test_empty_start(window):
    assert "Host hinzufügen" in window.empty_text.text()
    assert window.empty.isVisibleTo(window) and window.empty_button.isVisibleTo(window)
    assert not window.info.isVisibleTo(window)
    assert window.table.isHidden()
    assert not window.act_import.isEnabled()
    assert "zuerst einen Host auswählen" in window.act_import.toolTip()


def test_refresh_shows_status_before_import(qtbot, window, db):
    hid = db.add_host("srv-a", "vpn")
    window.reload_tree()
    window.refresh_all()
    _wait(qtbot, window)

    top = window.tree.topLevelItem(0)
    assert top.text(0) == "srv-a" and top.child(0).text(0) == "wg0 (nicht übernommen)"
    assert QFont(top.child(0).font(0)).italic()
    assert db.host(hid).os == "Debian GNU/Linux 13 (trixie)"

    window.select(hid, "wg0")
    assert "nur auf dem Server" in window.info.text()
    assert "gelesen um" in window.info.text()
    names = _column(window, "Name")
    assert names[:3] == ["laptop", "phone", "office"]
    assert names[3].startswith("Peer ") and names[3].endswith("…")  # unnamed peer: short key
    assert names[4] == f"Peer {short_key(K9)} (nur Laufzeit)"  # K9 is running but absent from the file
    assert _column(window, "PublicKey")[4] == K9
    # show status as text, not just color; fixture handshake is in the future, so active
    assert _column(window, "Status") == ["aktiv", "nie verbunden", "nicht geladen", "nicht geladen", "nie verbunden"]
    assert _column(window, "Endpoint")[0] == "203.0.113.5:40000"
    assert _column(window, "Empfangen")[0] == "1.0 KiB"


def test_import_from_server(qtbot, window, db):
    hid = db.add_host("srv-a", "vpn")
    window.reload_tree()
    window.select(hid)
    window.import_from_server()
    _wait(qtbot, window)

    assert window.confirmed and window.confirmed[0][0] == "srv-a"
    [iface] = db.interfaces(hid)
    assert {p.public_key for p in db.peers(iface.id)} >= {K1}
    window.select(hid, "wg0")
    assert window.tree.currentItem().text(0) == "wg0"
    assert not QFont(window.tree.currentItem().font(0)).italic()
    assert "in der App" in window.info.text()

    window.import_from_server()  # second run: nothing to do
    _wait(qtbot, window)
    assert "srv-a: App und Server stimmen überein" in window.statusBar().currentMessage()  # no pop-up


def test_import_cancelled(qtbot, db):
    w = MainWindow(db, runner_factory=lambda host: debian_runner(), confirm_import=lambda *a: False)
    qtbot.addWidget(w)
    hid = db.add_host("srv-a", "vpn")
    w.reload_tree()
    w.select(hid)
    w.import_from_server()
    qtbot.waitUntil(lambda: not w.bg.busy, timeout=5000)
    assert db.interfaces(hid) == []


def test_test_connection(qtbot, window, db):
    hid = db.add_host("srv-a", "vpn")
    window.reload_tree()
    window.select(hid)
    window.test_connection()
    _wait(qtbot, window)
    assert window.messages == []  # report success without a dialog (KDE HIG)
    assert "getestet um" in window.info.text() and "Debian" in window.info.text()
    assert "srv-a ok" in window.statusBar().currentMessage()
    assert window.hints.text() == ""


def test_test_connection_shows_problems_inline(qtbot, window, db):
    runner = (FakeRunner().on(r"os-release", 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n')
              .on(r"^id -u$", "1000\n").on(r"command -v", "systemctl\n"))  # neither root nor wg
    window.runner_factory = lambda host: runner
    hid = db.add_host("srv-a", "vpn")
    window.reload_tree()
    window.select(hid)
    window.test_connection()
    _wait(qtbot, window)
    assert window.hints.kind == "warning"
    assert "Kein root-Zugriff" in window.hints.text() and "wireguard-tools fehlen" in window.hints.text()


def test_actions_locked_while_reading(qtbot, window, db):
    hid = db.add_host("srv-a", "vpn")
    window.reload_tree()
    window.select(hid)
    window.refresh_all()
    assert hid in window.busy
    assert not window.act_import.isEnabled() and not window.act_delete.isEnabled()
    assert "wird gerade gelesen" in window.act_test.toolTip()
    assert window.progress.isVisibleTo(window)
    assert "Lese srv-a" in window.statusBar().currentMessage()
    window.refresh_all()  # second click does not start another call
    _wait(qtbot, window)
    assert window.act_import.isEnabled() and not window.progress.isVisibleTo(window)
    assert "Fertig um" in window.statusBar().currentMessage()


def test_add_host_reads_immediately(qtbot, window, db):
    window.ask_host = lambda host: ("srv-a", "root@vpn.example.org", None, False)
    window.add_host()
    _wait(qtbot, window)
    [host] = db.hosts()
    assert host.id in window.states  # read without another click
    assert window.tree.topLevelItem(0).childCount() == 1


def test_delete_host_asks_and_removes(qtbot, window, db):
    hid = db.add_host("srv-a", "vpn")
    window.reload_tree()
    window.select(hid)
    asked = []
    window.confirm_delete = lambda host: asked.append(host.name) or False
    window.delete_host()
    assert asked == ["srv-a"] and db.hosts()
    window.confirm_delete = lambda host: True
    window.delete_host()
    assert db.hosts() == []
    assert "Entfernen" not in [a.text() for a in window.findChildren(QToolBar)[0].actions()]


def test_copy_public_key(qtbot, window, db):
    hid = db.add_host("srv-a", "vpn")
    window.reload_tree()
    window.refresh_all()
    _wait(qtbot, window)
    window.select(hid, "wg0")
    assert not window.act_copy_key.isEnabled()
    window.table.selectRow(0)
    assert window.act_copy_key.isEnabled()
    window.copy_public_key()
    assert QGuiApplication.clipboard().text() == K1


def test_ssh_error_is_shown(qtbot, db):
    w = MainWindow(db, runner_factory=lambda host: FakeRunner())  # no rules: error
    qtbot.addWidget(w)
    hid = db.add_host("srv-a", "vpn")
    w.reload_tree()
    w.refresh_all()
    qtbot.waitUntil(lambda: not w.bg.busy, timeout=5000)
    w.select(hid)
    assert hid in w.errors
    assert w.bar.kind == "error" and "srv-a" in w.bar.text()
    assert w.bar.button.text() == "Protokoll anzeigen"


def test_host_dialog_validation(qtbot):
    dlg = HostDialog()
    qtbot.addWidget(dlg)
    assert not dlg.ok.isEnabled() and dlg.error.text() == ""  # empty: disabled, but no error text yet
    dlg.name.setText("srv-a")
    dlg.target.setText("-oProxyCommand=x")
    assert "SSH-Ziel" in dlg.error.text() and not dlg.ok.isEnabled()  # validate while typing
    dlg._accept()
    assert dlg.result() != HostDialog.DialogCode.Accepted
    dlg.target.setText("root@vpn.example.org")
    assert dlg.ok.isEnabled() and dlg.error.text() == ""
    dlg.port.setValue(2222)
    dlg._accept()
    assert dlg.result() == HostDialog.DialogCode.Accepted
    assert dlg.values() == ("srv-a", "root@vpn.example.org", 2222, False, None)  # None means system key


def test_import_dialog_lists_changes(qtbot):
    from wggui.db import Change

    dlg = ImportDialog("srv-a", [Change("neu", "Interface wg0", "10.98.0.1/24")])
    qtbot.addWidget(dlg)
    assert dlg.windowTitle() == "Vom Server übernehmen – srv-a"
    assert any("Auf dem Server wird nichts geändert" in label.text() for label in dlg.findChildren(QLabel))
    [tree] = dlg.findChildren(QTreeWidget)
    item = tree.topLevelItem(0)
    assert [item.text(i) for i in range(3)] == ["neu", "Interface wg0", "10.98.0.1/24"]


def test_log_dock_shows_commands_and_opens_on_error(qtbot, db):
    w = MainWindow(db, runner_factory=lambda host: FakeRunner(label="srv-a"))  # no rules: error
    qtbot.addWidget(w)
    db.add_host("srv-a", "vpn")
    w.reload_tree()
    assert not w.log_dock.isVisibleTo(w)
    w.refresh_all()
    qtbot.waitUntil(lambda: not w.bg.busy, timeout=5000)
    qtbot.waitUntil(lambda: any("Fehler" in line or "→ 127" in line for line in w.log_dock.lines()), timeout=2000)
    assert not w.log_dock.isVisibleTo(w)  # no longer opens itself (would take half the window)
    assert "Details im Protokoll" in w.statusBar().currentMessage()
    w.select(w.db.hosts()[0].id)
    assert w.bar.button.text() == "Protokoll anzeigen" and "Fehler" in w.tree.topLevelItem(0).text(0)
    w.bar.button.click()
    assert w.log_dock.isVisibleTo(w)
    w.close()  # unregister handler
    import logging

    assert w.log_dock.handler not in logging.getLogger("wggui").handlers


def test_host_dialog_sudo(qtbot, db):
    hid = db.add_host("srv-a", "admin@vpn.example.org", sudo=True)
    dlg = HostDialog(host=db.host(hid))
    qtbot.addWidget(dlg)
    assert dlg.sudo.isChecked()
    assert dlg.values()[3] is True


def test_default_runner_uses_sudo(db):
    from wggui.ui.main_window import _default_runner

    hid = db.add_host("srv-a", "admin@vpn.example.org", sudo=True)
    assert _default_runner(db.host(hid)).sudo is True


def test_qt_translation_german(qapp):
    """German standard buttons (otherwise “Cancel” appears beside “Speichern”). Skip if qtbase_de is missing."""
    from PySide6.QtCore import QLocale
    from PySide6.QtWidgets import QDialogButtonBox

    from wggui.app import install_qt_translation

    previous = QLocale()
    QLocale.setDefault(QLocale("de_DE"))
    try:
        if not install_qt_translation(qapp):
            pytest.skip("qtbase_de.qm nicht installiert")
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        assert box.buttons()[0].text() == "Abbrechen"
    finally:
        QLocale.setDefault(previous)
