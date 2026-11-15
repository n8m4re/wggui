"""Stage 2: add a client through the main window and dialogs (offscreen, FakeRunner)."""

import stat

import pytest
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QPlainTextEdit

from tests.conftest import debian_runner
from tests.keys import PUB0, SECRETS
from wggui import apply
from wggui.client import ClientSpec
from wggui.db import Database
from wggui.parse import parse_conf
from wggui.remote import FakeRunner
from wggui.ui.dialogs import ClientDialog, PlanDialog, QrDialog, qr_png
from wggui.ui.main_window import MainWindow

HASH = "c" * 64


def client_runner(confirm_rc: int = 0, addresses: list[str] | None = None) -> FakeRunner:
    """Debian host and prepare/apply/confirm responses. Custom rules first (FakeRunner uses the first match)."""
    runner = debian_runner()
    extra = (FakeRunner()
             .on(r"wggui-new", "synced\nbackup /etc/wireguard/wg0.conf.wggui-x\n")  # APPLY
             .on(r'echo "confirmed"', "confirmed\n" if confirm_rc == 0 else "", confirm_rc,
                 "" if confirm_rc == 0 else "Keine offene Änderung")  # CONFIRM
             .on(r"running yes", f"sha256 {HASH}\nrunning yes\nsetsid yes\nwgquick yes\n"))  # PREPARE
    runner.rules[:0] = extra.rules
    runner.addresses = addresses or []
    return runner


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "gui.db")
    yield d
    d.close()


@pytest.fixture
def window(qtbot, db):
    runner = client_runner()
    w = MainWindow(db, runner_factory=lambda host: w.runner, confirm_import=lambda *a: True)
    w.runner = runner
    w.messages, w.asked, w.plans, w.shown = [], [], [], []
    w.message = lambda title, text: w.messages.append((title, text))
    w.error = lambda title, text: w.messages.append((title, text))

    def ask_client(title, iface_addresses, used, suggested, endpoint, dns):
        w.asked.append((title, suggested, endpoint))
        return ClientSpec("handy", suggested, "vpn.example.org:51820", full_tunnel=False, dns=["10.98.0.1"])

    w.ask_client = ask_client
    w.confirm_write = lambda plan: w.plans.append(plan) or True
    w.show_client = lambda name, text: w.shown.append((name, text))
    qtbot.addWidget(w)
    return w


def _wait(qtbot, w):
    qtbot.waitUntil(lambda: not w.bg.busy, timeout=5000)


def _imported(qtbot, w, db) -> int:
    hid = db.add_host("srv-a", "root@vpn.example.org")
    w.reload_tree()
    w.select(hid)
    w.import_from_server()
    _wait(qtbot, w)
    w.select(hid, "wg0")
    return hid


def test_action_needs_imported_interface(qtbot, window, db):
    hid = db.add_host("srv-a", "root@vpn.example.org")
    window.reload_tree()
    window.select(hid)
    assert not window.act_client.isEnabled() and "Interface auswählen" in window.act_client.toolTip()
    window.refresh_all()
    _wait(qtbot, window)
    window.select(hid, "wg0")
    assert not window.act_client.isEnabled() and "Vom Server übernehmen" in window.act_client.toolTip()
    window.select(hid)
    window.import_from_server()
    _wait(qtbot, window)
    window.select(hid, "wg0")
    assert window.act_client.isEnabled()


def test_add_client_full_flow(qtbot, window, db):
    hid = _imported(qtbot, window, db)
    window.add_client()
    _wait(qtbot, window)

    [(title, suggested, endpoint)] = window.asked
    assert title == "srv-a / wg0"
    assert suggested == ["10.98.0.6/32"]  # .2–.5 and .9 are taken
    assert endpoint == "vpn.example.org:51820"  # from SSH target and ListenPort
    [plan] = window.plans
    assert plan.conf_hash == HASH and plan.running and plan.warnings == ()
    assert "PresharedKey = (neu" in plan.preview

    apply_call = next(c for c in window.runner.calls if "wggui-new" in " ".join(c.argv))
    assert "PresharedKey = " in apply_call.stdin and "(neu" not in apply_call.stdin
    psk = apply_call.stdin.split("PresharedKey = ", 1)[1].split()[0]
    assert all(psk not in arg for arg in apply_call.argv)
    assert any('echo "confirmed"' in " ".join(c.argv) for c in window.runner.calls)

    [(name, text)] = window.shown
    conf = parse_conf(text)
    assert name == "handy" and conf.has_private_key and conf.addresses == ["10.98.0.6/32"]
    [peer] = conf.peers
    assert peer.public_key == PUB0 and peer.has_psk and peer.endpoint == "vpn.example.org:51820"
    private = text.split("PrivateKey = ", 1)[1].split()[0]

    [iface] = db.interfaces(hid)
    assert iface.client_endpoint == "vpn.example.org:51820" and iface.client_dns == "10.98.0.1"
    handy = next(p for p in db.peers(iface.id) if p.name == "handy")
    assert handy.allowed_ips == ["10.98.0.6/32"] and handy.has_psk
    # no secrets in the database or log
    dump = "\n".join(db.conn.iterdump())
    log_text = "\n".join(window.log_dock.lines())
    for secret in (private, psk, *SECRETS):
        assert secret not in dump and secret not in log_text


def test_warning_when_ssh_runs_over_interface(qtbot, window, db):
    window.runner = client_runner(addresses=["10.98.0.1"])
    _imported(qtbot, window, db)
    window.add_client()
    _wait(qtbot, window)
    [plan] = window.plans
    assert any("eigenen Zugang" in w for w in plan.warnings)


def test_cancel_at_plan_writes_nothing(qtbot, window, db):
    hid = _imported(qtbot, window, db)
    window.confirm_write = lambda plan: False
    window.add_client()
    _wait(qtbot, window)
    assert not any("wggui-new" in " ".join(c.argv) for c in window.runner.calls)
    [iface] = db.interfaces(hid)
    assert "handy" not in [p.name for p in db.peers(iface.id)]
    assert window.shown == []


def test_not_confirmed_shows_rollback_hint(qtbot, window, db):
    hid = _imported(qtbot, window, db)
    window.runner = client_runner(confirm_rc=5)
    window.add_client()
    _wait(qtbot, window)
    assert window.shown == []
    [iface] = db.interfaces(hid)
    assert "handy" not in [p.name for p in db.peers(iface.id)]
    window.select(hid, "wg0")
    assert window.bar.kind == "error"
    assert "nicht bestätigt" in window.bar.text() and "zurück" in window.bar.text()


def test_client_dialog_validates_live(qtbot):
    dlg = ClientDialog("srv-a / wg0", ["10.98.0.1/24"], ["10.98.0.2/32"], ["10.98.0.3/32"],
                       "vpn.example.org:51820", "")
    qtbot.addWidget(dlg)
    assert dlg.address.text() == "10.98.0.3/32"
    assert not dlg.ok.isEnabled() and dlg.error.text() == ""  # name is missing, but no error text yet
    dlg.name.setText("handy")
    assert dlg.ok.isEnabled()
    dlg.address.setText("10.98.0.2")
    assert "vergeben" in dlg.error.text() and not dlg.ok.isEnabled()
    dlg.address.setText("10.98.0.7")
    dlg.full.setChecked(True)
    assert dlg.full_hint.isVisibleTo(dlg)
    spec = dlg.spec()
    assert spec.addresses == ["10.98.0.7/32"] and spec.full_tunnel and spec.psk and spec.keepalive


def test_plan_dialog_names_effect_and_defaults_to_cancel(qtbot):
    plan = apply.WritePlan("srv-a", "wg0", "/etc/wireguard", "+# handy\n+[Peer]\n+PublicKey = x\n", "geheim", HASH,
                           True, "20261115-120000", warnings=("SSH läuft über wg0",))
    dlg = PlanDialog(plan)
    qtbot.addWidget(dlg)
    assert dlg.ok.text() == "Auf srv-a schreiben"
    buttons = dlg.findChild(QDialogButtonBox)
    assert buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault()
    shown = [w.text() for w in dlg.findChildren(QLabel)] + [dlg.findChild(QPlainTextEdit).toPlainText()]
    assert "+[Peer]" in shown[-1] and not any("geheim" in t for t in shown)  # preview only, never the block
    assert any("SSH läuft über wg0" in t for t in shown)


def test_qr_dialog_save_and_discard(qtbot, tmp_path):
    assert qr_png("[Interface]\n").startswith(b"\x89PNG")
    dlg = QrDialog("handy", "[Interface]\nPrivateKey = geheim\n")
    qtbot.addWidget(dlg)
    dlg.confirm_discard = lambda: False
    dlg.show()
    dlg.reject()
    assert dlg.isVisible()  # unsaved and discard declined: keep dialog open
    target = tmp_path / "handy.conf"
    dlg.ask_save_path = lambda suggestion: str(target)
    assert dlg.save()
    assert target.read_text() == "[Interface]\nPrivateKey = geheim\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    dlg.reject()
    assert not dlg.isVisible()
