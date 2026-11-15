"""GUI review fixes (Codex, 2026-11-15): selection, filters, sorting, drift, reconciliation order,
status bar, inline errors, host name, QR save errors, client defaults."""

import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from tests.gui import test_peer_flow as flow
from tests.gui.test_peer_flow import _peer, _select_peer, _wait
from tests.keys import K1, K3
from wggui import sync
from wggui.client import ClientSpec
from wggui.ui.dialogs import HostDialog, QrDialog
from wggui.ui.main_window import PEER_COLUMNS

db = flow.db  # same fixtures as stage 3 tests (host with imported wg0)
window = flow.window


def _row_of(w, public_key):
    col = PEER_COLUMNS.index("PublicKey")
    return next(r for r in range(w.table.rowCount()) if w.table.item(r, col).text() == public_key)


def test_selection_survives_refresh_and_filter_sort(qtbot, window):
    _select_peer(window, K3)
    window.refresh_all()
    _wait(qtbot, window)
    assert window._selected_key() == K3

    window.filter.setText("office")
    visible = [r for r in range(window.table.rowCount()) if not window.table.isRowHidden(r)]
    assert len(visible) == 1
    window.filter.clear()

    window.table.sortByColumn(PEER_COLUMNS.index("Empfangen"), Qt.SortOrder.DescendingOrder)
    assert window.table.item(0, PEER_COLUMNS.index("Name")).text() == "laptop"  # 1.0 KiB > 0 B > „–“
    assert window.act_find.shortcut().matches(QKeySequence(QKeySequence.StandardKey.Find)) \
        == QKeySequence.SequenceMatch.ExactMatch


def test_drift_is_marked_in_table(window, db):
    db.conn.execute("UPDATE peer SET name = 'laptop-app' WHERE public_key = ?", (K1,))
    db.conn.commit()
    window.reload_tree()
    window.select(window.hid, "wg0")
    assert window.drift_bar.kind == "warning" and "Abweichung" in window.drift_bar.text()
    assert window.drift_bar.button.text() == "Abgleichen…"
    assert "Weicht vom Server ab" in window.table.item(_row_of(window, K1), 0).toolTip()


def test_sync_app_changes_wait_for_server_write(qtbot, window, db):
    db.conn.execute("UPDATE peer SET name = 'laptop-app' WHERE public_key = ?", (K1,))
    db.conn.execute("DELETE FROM peer WHERE public_key = ?", (K3,))
    db.conn.commit()

    def ask_sync(title, drifts):
        pick = {}
        for i, d in enumerate(drifts):
            target = getattr(d, "key")  # noqa: B009
            pick[i] = sync.TO_SERVER if target == K1 else sync.TO_APP if target == K3 else sync.IGNORE
        return pick

    window.ask_sync = ask_sync
    window.confirm_write = lambda plan: False
    window.select(window.hid, "wg0")
    window.sync_interface()
    _wait(qtbot, window)
    assert _peer(db, window.iface_id, K3) is None  # preview canceled: app unchanged

    window.confirm_write = lambda plan: True
    window.sync_interface()
    _wait(qtbot, window)
    assert _peer(db, window.iface_id, K3) is not None  # imported after successful write


def test_status_says_writing(qtbot, window):
    _select_peer(window, K3)
    messages = []
    window.statusBar().messageChanged.connect(messages.append)
    window.remove_peer()
    _wait(qtbot, window)
    assert any(m.startswith("Schreibe auf srv-a") for m in messages)


def test_missing_peer_error_is_inline(qtbot, window):
    _select_peer(window, K3)
    only_interface = "[Interface]\nAddress = 10.98.0.1/24\n"
    window.runner.rules.insert(0, (re.compile(r"presharedkey.*wg0\.conf"), only_interface, 0, ""))
    window.remove_peer()
    _wait(qtbot, window)
    assert window.messages == []  # no pop-up
    assert window.bar.kind == "error" and "nicht (mehr) in der Server-Datei" in window.bar.text()


def test_host_dialog_rejects_taken_name(qtbot, db):
    dlg = HostDialog(taken={"srv-a"})
    qtbot.addWidget(dlg)
    dlg.name.setText("srv-a")
    dlg.target.setText("root@vpn.example.org")
    assert "schon vergeben" in dlg.error.text() and not dlg.ok.isEnabled()
    hid = db.add_host("srv-a", "vpn")
    own = HostDialog(host=db.host(hid), taken={"srv-a"})  # own name is allowed
    qtbot.addWidget(own)
    assert own.ok.isEnabled()


def test_qr_save_error_is_shown_in_dialog(qtbot, tmp_path):
    dlg = QrDialog("handy", "[Interface]\n")
    qtbot.addWidget(dlg)
    dlg.ask_save_path = lambda suggestion: str(tmp_path / "fehlt" / "handy.conf")
    assert not dlg.save()
    assert dlg.save_error.kind == "error" and "Nicht gespeichert" in dlg.save_error.text()
    assert dlg.saved_to is None


def test_client_defaults_saved_only_after_success(qtbot, window, db):
    window.ask_client = lambda *a: ClientSpec("handy", ["10.98.0.6/32"], "vpn.example.org:51820", False)
    window.confirm_write = lambda plan: False
    window.select(window.hid, "wg0")
    window.add_client()
    _wait(qtbot, window)
    [iface] = db.interfaces(window.hid)
    assert iface.client_endpoint is None  # canceled: default not remembered


def test_auto_refresh(qtbot, window):
    from wggui import settings

    assert not window.act_auto.isChecked() and not window.auto_timer.isActive()
    window.act_auto.setChecked(True)
    assert window.auto_timer.isActive() and settings.get_bool("auto_refresh")
    calls = len(window.runner.calls)
    window._auto_refresh()
    _wait(qtbot, window)
    assert len(window.runner.calls) > calls  # reads without a click
    window.busy[999] = "Lese x"
    calls = len(window.runner.calls)
    window._auto_refresh()  # operation in progress: do nothing
    assert len(window.runner.calls) == calls
    window.busy.pop(999)
    window.act_auto.setChecked(False)
    assert not window.auto_timer.isActive()


def test_prune_backups_flow(qtbot, window):
    tags = [f"2026111{i}-120000" for i in range(7)]
    window.runner.rules.insert(0, (re.compile(r"removed"), "".join(f"removed {t}\n" for t in tags[:2]), 0, ""))
    window.runner.rules.insert(0, (re.compile(r"wggui-\[0-9\]"), "\n".join(tags) + "\n", 0, ""))
    asked = []
    window.confirm_prune = lambda host, files, kept: asked.append((files, kept)) or True
    window.select(window.hid, "wg0")
    window.prune_backups()
    _wait(qtbot, window)
    [(files, kept)] = asked
    assert files == [f"/etc/wireguard/wg0.conf.wggui-{t}" for t in tags[:2]] and kept == 5
    prune = next(c for c in window.runner.calls if "removed" in " ".join(c.argv))
    assert prune.argv[-2:] == tags[:2]
    assert "2 Backups gelöscht" in window.statusBar().currentMessage()
