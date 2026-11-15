"""Stage 4 in the main window: install wireguard-tools, new interface, server-to-server tunnel (FakeRunner)."""

import re

from tests.gui import test_peer_flow as flow
from tests.gui.test_peer_flow import _wait
from tests.keys import K1, PUB0, fake_key
from wggui.ui.provision_dialogs import NewInterfaceDialog, TunnelDialog, TunnelEnd

db = flow.db
window = flow.window


def test_install_tools_asks_and_runs(qtbot, window):
    window.runner.rules.insert(0, (re.compile(r"command -v \"\$c\""), "systemctl\n", 0, ""))  # wg missing
    window.runner.rules.insert(0, (re.compile(r"apt-get install"), "", 0, ""))
    asked = []
    window.confirm_install = lambda host, command: asked.append(command) or True
    window.select(window.hid)
    window.install_tools()
    _wait(qtbot, window)
    assert asked and "apt-get install -y -q wireguard-tools" in asked[0]
    assert any("apt-get install" in " ".join(c.argv) for c in window.runner.calls)


def test_new_interface_flow(qtbot, window):
    window.runner.rules.insert(0, (re.compile(r"wg genkey"), f"started\npubkey {K1}\n", 0, ""))
    seen = []

    def ask(host, existing, systemd):
        seen.append((sorted(existing), systemd))
        from wggui.provision import NewInterface
        return NewInterface(host.name, "wg1", "10.98.1.1/24", 51821, enable=False)

    window.ask_new_interface = ask
    window.select(window.hid)
    window.new_interface()
    _wait(qtbot, window)
    assert seen == [(["wg0"], True)]
    [plan] = window.plans
    assert "verlässt ihn nie" in plan.preview and plan.conf_path == "/etc/wireguard/wg1.conf"
    call = next(c for c in window.runner.calls if "wg genkey" in " ".join(c.argv))
    assert call.stdin is None
    assert "Interface wg1 angelegt" in window.statusBar().currentMessage()


def test_tunnel_flow_writes_both_sides(qtbot, window, db):
    other = db.add_host("vpn2", "root@vpn2.example.org")
    db.conn.execute("INSERT INTO interface (host_id, name, addresses, listen_port, public_key) "
                    "VALUES (?, 'wg0', '10.98.5.1/24', 51820, ?)", (other, fake_key(0x55)))
    db.conn.commit()
    window.reload_tree()
    window.select(window.hid, "wg0")
    chosen = []

    def ask(own, others):
        chosen.append((own.label, [o.label for o in others]))
        dlg = TunnelDialog(own, others)
        qtbot.addWidget(dlg)
        return dlg.value()

    window.ask_tunnel = ask
    window.new_tunnel()
    _wait(qtbot, window)
    [(own_label, other_labels)] = chosen
    assert own_label.endswith("/ wg0") and other_labels == ["vpn2 / wg0"]
    assert [p.host_name for p in window.plans] == [window.db.host(window.hid).name, "vpn2"]
    blocks = [c.stdin for c in window.runner.calls if "wggui-new" in " ".join(c.argv)]
    assert len(blocks) == 2 and fake_key(0x55) in blocks[0] and PUB0 in blocks[1]
    psk_a = blocks[0].split("PresharedKey = ")[1].split()[0]
    assert f"PresharedKey = {psk_a}" in blocks[1]  # same PSK on both sides
    assert all(psk_a not in p.preview for p in window.plans)


def test_new_interface_dialog_validates(qtbot):
    dlg = NewInterfaceDialog("vpn1", {"wg0": (["10.98.0.1/24"], 51820)}, systemd=False)
    qtbot.addWidget(dlg)
    assert dlg.name.text() == "wg1" and dlg.port.value() == 51821 and not dlg.enable.isEnabled()
    assert dlg.ok.isEnabled()
    dlg.address.setText("10.98.0.7/24")
    assert "überschneidet" in dlg.error.text() and not dlg.ok.isEnabled()


def test_tunnel_dialog_defaults(qtbot):
    own = TunnelEnd(1, "vpn1", "wg0", PUB0, ("10.98.0.1/24",), "vpn1.example.org:51820")
    other = TunnelEnd(2, "vpn2", "wg0", K1, ("10.98.5.1/24",), "vpn2.example.org:51820")
    dlg = TunnelDialog(own, [other])
    qtbot.addWidget(dlg)
    assert dlg.allowed_a.text() == "10.98.0.1/32" and dlg.allowed_b.text() == "10.98.5.1/32"
    assert dlg.endpoint_b.text() == "vpn2.example.org:51820" and dlg.ok.isEnabled()
    dlg.allowed_b.setText("kein-netz")
    assert not dlg.ok.isEnabled()
