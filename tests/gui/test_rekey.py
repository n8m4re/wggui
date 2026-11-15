"""Issue a new QR code for an existing peer (rekey): new keys, same name and address."""

from tests.conftest import fixture_text, redact_conf
from tests.gui import test_peer_flow as flow
from tests.gui.test_peer_flow import _apply_call, _peer, _select_peer, _wait
from tests.keys import K2, K4, PSK, PUB0, SECRETS
from wggui import keys
from wggui.client import ClientSpec
from wggui.edit import PeerOp, order_text, transform_local
from wggui.parse import parse_conf
from wggui.ui.dialogs import ClientDialog

db = flow.db
window = flow.window


def test_rekey_awk_replaces_key_and_psk_only():
    conf = fixture_text("wg0.conf")
    pair, psk = keys.generate_keypair(), keys.generate_psk()
    op = PeerOp("rekey", K2, new_key=pair.public, psk=psk)
    new = transform_local(redact_conf(conf), [op])
    peer = next(p for p in parse_conf(new).peers if p.public_key == pair.public)
    assert peer.name == "phone" and peer.allowed_ips == ["10.98.0.3/32", "192.0.2.0/24"] and peer.has_psk
    assert K2 not in new and psk not in new  # preview shows only “(redacted)” for PSK
    assert psk in order_text([op]) and psk not in order_text([op], preview=True) and psk not in repr(op)
    without = transform_local(conf, [PeerOp("rekey", K2, new_key=pair.public)])
    assert PSK not in without  # old PSK is dropped when no new one is requested


def test_rekey_flow(qtbot, window, db):
    _select_peer(window, K2)
    asked = []

    def ask_client(title, iface_addresses, used, suggested, endpoint, dns, fixed_name=None):
        asked.append((suggested, fixed_name, used))
        return ClientSpec(fixed_name, suggested, endpoint, full_tunnel=False)

    shown = []
    window.ask_client = ask_client
    window.show_client = lambda name, text: shown.append((name, text))
    window.rekey_peer()
    _wait(qtbot, window)

    [(suggested, fixed_name, used)] = asked
    assert suggested == ["10.98.0.3/32"] and fixed_name == "phone"  # own address, not 192.0.2.0/24
    assert "10.98.0.3/32" not in used
    [plan] = window.plans
    assert any("neue Schlüssel" in w for w in plan.warnings)
    call = _apply_call(window)
    new_psk = call.stdin.rstrip("\n").split("\t")[-1]
    assert len(new_psk) == 44 and all(new_psk not in a for a in call.argv) and new_psk not in plan.preview

    [(name, text)] = shown
    conf = parse_conf(text)
    assert name == "phone" and conf.addresses == ["10.98.0.3/32"] and conf.peers[0].public_key == PUB0
    assert f"PresharedKey = {new_psk}" in text
    new_key = call.stdin.split("\t")[6]
    assert _peer(db, window.iface_id, K2) is None and _peer(db, window.iface_id, new_key).name == "phone"
    for secret in (new_psk, text.split("PrivateKey = ")[1].split()[0], *SECRETS):
        assert secret not in "\n".join(db.conn.iterdump()) and secret not in "\n".join(window.log_dock.lines())


def test_rekey_needs_own_address(qtbot, window):
    window.runner.rules.insert(0, (__import__("re").compile(r"presharedkey.*wg0\.conf"),
                                   "[Interface]\nAddress = 10.98.0.1/24\n\n[Peer]\nPublicKey = " + K4 +
                                   "\nAllowedIPs = 198.51.100.0/24\n", 0, ""))
    _select_peer(window, K4)
    window.ask_client = lambda *a, **k: (_ for _ in ()).throw(AssertionError("kein Dialog"))
    window.rekey_peer()
    _wait(qtbot, window)
    assert window.bar.kind == "error" and "keine einzelne Adresse" in window.bar.text()


def test_client_dialog_rekey_mode(qtbot):
    dlg = ClientDialog("srv-a / wg0", ["10.98.0.1/24"], ["10.98.0.2/32"], ["10.98.0.3/32"], "vpn.example.org:51820",
                       "", fixed_name="Peer AgICAgIC…")
    qtbot.addWidget(dlg)
    assert dlg.name.isReadOnly() and dlg.address.isReadOnly() and dlg.ok.isEnabled()
    assert dlg.rekey_warning.kind == "warning" and "neue Schlüssel" in dlg.rekey_warning.text()
    assert dlg.windowTitle().startswith("Neuen QR-Code ausstellen")
