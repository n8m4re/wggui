"""Stage 3: edit, disable, remove, and reconcile peers in the main window (offscreen, FakeRunner)."""

import pytest

from tests.gui.test_client_flow import client_runner
from tests.keys import K1, K2, K3, K4, PUB0, SECRETS, fake_key
from wggui import sync
from wggui.apply import BUILD_COPY, BUILD_EDIT
from wggui.db import Database
from wggui.edit import EDIT_AWK, PeerOp
from wggui.parse import PeerConf
from wggui.ui.dialogs import PeerDialog, SyncDialog
from wggui.ui.main_window import PEER_COLUMNS, MainWindow

OLD_KEY = fake_key(0x40)  # app only


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "gui.db")
    yield d
    d.close()


@pytest.fixture
def window(qtbot, db):
    w = MainWindow(db, runner_factory=lambda host: w.runner, confirm_import=lambda *a: True)
    w.runner = client_runner()
    w.messages, w.plans = [], []
    w.message = lambda title, text: w.messages.append((title, text))
    w.error = lambda title, text: w.messages.append((title, text))
    w.confirm_write = lambda plan: w.plans.append(plan) or True
    qtbot.addWidget(w)
    hid = db.add_host("srv-a", "root@vpn.example.org")
    w.reload_tree()
    w.select(hid)
    w.import_from_server()
    qtbot.waitUntil(lambda: not w.bg.busy, timeout=5000)
    w.hid = hid
    w.iface_id = db.interfaces(hid)[0].id
    return w


def _wait(qtbot, w):
    qtbot.waitUntil(lambda: not w.bg.busy, timeout=5000)


def _select_peer(w, key):
    w.select(w.hid, "wg0")
    col = PEER_COLUMNS.index("PublicKey")
    row = next(r for r in range(w.table.rowCount()) if w.table.item(r, col).text() == key)
    w.table.selectRow(row)


def _apply_call(w):
    return next(c for c in w.runner.calls if "wggui-new" in " ".join(c.argv))


def _peer(db, iface_id, key):
    return next((p for p in db.peers(iface_id) if p.public_key == key), None)


def test_peer_actions_need_selection(window):
    window.select(window.hid, "wg0")
    window.table.clearSelection()
    assert not window.act_peer_edit.isEnabled() and "Peer in der Tabelle" in window.act_peer_edit.toolTip()
    _select_peer(window, K1)
    assert window.act_peer_edit.isEnabled() and window.act_peer_toggle.text() == "Peer sperren…"


def test_edit_peer_writes_diff_and_updates_app(qtbot, window, db):
    _select_peer(window, K2)
    seen = []

    def ask_peer(title, peer, others):
        seen.append((peer.name, peer.has_psk, sorted(others)))
        return PeerOp("edit", K2, "handy-neu", ("10.98.0.3/32",), 25)

    window.ask_peer = ask_peer
    window.edit_peer()
    _wait(qtbot, window)
    assert seen[0][:2] == ("phone", True) and "laptop" in seen[0][2]
    [plan] = window.plans
    assert "+# handy-neu" in plan.preview and "-### begin phone ###" in plan.preview
    call = _apply_call(window)
    assert BUILD_EDIT in call.argv and EDIT_AWK in call.argv
    assert call.stdin.startswith(f"edit\t{K2}\thandy-neu\t10.98.0.3/32\t25")
    for secret in SECRETS:
        assert secret not in call.stdin and all(secret not in a for a in call.argv)
    peer = _peer(db, window.iface_id, K2)
    assert (peer.name, peer.allowed_ips, peer.keepalive, peer.has_psk) == ("handy-neu", ["10.98.0.3/32"], 25, True)


def test_toggle_and_remove(qtbot, window, db):
    _select_peer(window, K1)
    window.toggle_peer()
    _wait(qtbot, window)
    [plan] = window.plans
    assert "+#wggui-off [Peer]" in plan.preview and any("nicht mehr verbinden" in w for w in plan.warnings)
    assert _peer(db, window.iface_id, K1).disabled
    _select_peer(window, K1)
    assert window.act_peer_toggle.text() == "Peer entsperren…"
    status = window.table.item(window.table.currentRow(), PEER_COLUMNS.index("Status")).text()
    assert status == "gesperrt"

    _select_peer(window, K3)
    window.remove_peer()
    _wait(qtbot, window)
    assert "-# Name = office" in window.plans[-1].preview
    assert _peer(db, window.iface_id, K3) is None


def test_cancel_writes_nothing(qtbot, window, db):
    window.confirm_write = lambda plan: False
    _select_peer(window, K3)
    window.remove_peer()
    _wait(qtbot, window)
    assert not any("wggui-new" in " ".join(c.argv) for c in window.runner.calls)
    assert _peer(db, window.iface_id, K3) is not None


def test_sync_fixture_has_only_runtime_drift(qtbot, window):
    asked = []
    window.ask_sync = lambda title, drifts: asked.extend(drifts) or None
    window.select(window.hid, "wg0")
    window.sync_interface()
    _wait(qtbot, window)
    [drift] = asked  # import aligned app and file; K9 runs only, two peers are not loaded
    assert drift.kind == "laufzeit" and drift.default == sync.IGNORE
    assert "nur in der Laufzeit" in drift.detail and "nicht geladen" in drift.detail
    assert window.plans == []


def test_sync_without_drift_needs_no_dialog(qtbot, window):
    lines = [f"wg0\t(redacted)\t{PUB0}\t51820\toff"]
    peers = ((K1, "10.98.0.2/32", "off"), (K2, "10.98.0.3/32,192.0.2.0/24", "off"), (K3, "10.98.0.4/32", "25"),
             (K4, "10.98.0.5/32", "off"))
    lines += [f"wg0\t{k}\t(none)\t(none)\t{ips}\t0\t0\t0\t{ka}" for k, ips, ka in peers]
    runner = client_runner()
    dump_rule = next(i for i, r in enumerate(runner.rules) if r[0].pattern == r"wg show all dump")
    runner.rules[dump_rule] = (runner.rules[dump_rule][0], "\n".join(lines) + "\n", 0, "")
    window.runner = runner
    window.ask_sync = lambda title, drifts: pytest.fail(f"kein Dialog ohne Abweichung: {drifts}")
    window.select(window.hid, "wg0")
    window.sync_interface()
    _wait(qtbot, window)
    assert "stimmen überein" in window.statusBar().currentMessage()


def test_sync_directions(qtbot, window, db):
    # app differs: renamed peer and one app-only peer
    db.conn.execute("UPDATE peer SET name = 'laptop-app' WHERE public_key = ?", (K1,))
    db.add_peer(window.iface_id, "alt", OLD_KEY,
                ["10.98.0.40/32"], False)
    db.conn.commit()
    asked = []

    def ask_sync(title, drifts):
        asked.extend(drifts)
        choice = {}
        for i, d in enumerate(drifts):
            if d.key == K1:
                choice[i] = sync.TO_SERVER
            elif d.kind == "nur_app":
                choice[i] = sync.REMOVE_APP
            elif d.kind == "laufzeit":
                choice[i] = sync.IGNORE
        return choice

    window.ask_sync = ask_sync
    window.select(window.hid, "wg0")
    window.sync_interface()
    _wait(qtbot, window)
    kinds = {d.kind for d in asked}
    assert {"unterschied", "nur_app"} <= kinds
    [plan] = window.plans
    assert "+# laptop-app" in plan.preview
    assert _peer(db, window.iface_id, OLD_KEY) is None  # removed from app


def test_sync_runtime_only_uses_copy_builder(qtbot, window):
    def ask_sync(title, drifts):
        return {i: (sync.SYNC_RUNTIME if d.kind == "laufzeit" else sync.IGNORE) for i, d in enumerate(drifts)}

    window.ask_sync = ask_sync
    window.select(window.hid, "wg0")
    window.sync_interface()
    _wait(qtbot, window)
    [plan] = window.plans
    assert plan.builder == BUILD_COPY and "unverändert" in plan.preview


def test_peer_dialog_validates(qtbot):
    peer = PeerConf(K1, "laptop", ["10.98.0.2/32"])
    dlg = PeerDialog("srv-a / wg0", peer, {"phone": ["10.98.0.3/32", "192.0.2.0/24"]})
    qtbot.addWidget(dlg)
    assert dlg.ok.isEnabled()
    dlg.allowed.setText("10.98.0.2/32, 192.0.2.128/25")
    assert "überschneiden" in dlg.error.text() and "phone" in dlg.error.text() and not dlg.ok.isEnabled()
    dlg.allowed.setText("10.98.0.2/32, 198.51.100.0/24")
    dlg.keepalive.setValue(25)
    op = dlg.op()
    assert op.allowed_ips == ("10.98.0.2/32", "198.51.100.0/24") and op.keepalive == 25 and op.name == "laptop"


def test_sync_dialog_summary(qtbot):
    drifts = [sync.Drift(K1, "laptop", "unterschied", "Name …", (sync.TO_APP, sync.TO_SERVER, sync.IGNORE),
                         sync.TO_APP)]
    dlg = SyncDialog("srv-a / wg0", drifts)
    qtbot.addWidget(dlg)
    assert dlg.ok.text() == "In die App übernehmen"
    dlg.boxes[0].setCurrentText(sync.TO_SERVER)
    assert dlg.ok.text() == "Weiter zur Vorschau…" and dlg.choices() == {0: sync.TO_SERVER}
    dlg.boxes[0].setCurrentText(sync.IGNORE)
    assert not dlg.ok.isEnabled()
