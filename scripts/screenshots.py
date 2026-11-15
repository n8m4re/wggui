#!/usr/bin/env python3
"""Create README/AppStream screenshots offscreen with synthetic data (example.org, 10.98.0.0/24, test keys).

  uv run python scripts/screenshots.py [ausgabeordner] [--lang en]

No real servers needed: FakeRunner supplies config and `wg show` output.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from PySide6.QtCore import QLocale  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleFactory  # noqa: E402

from tests.keys import fake_key  # noqa: E402
from wggui import apply, client, edit, i18n, keys, sync  # noqa: E402
from wggui.app import install_qt_translation  # noqa: E402
from wggui.db import Database  # noqa: E402
from wggui.discover import REDACTED  # noqa: E402
from wggui.parse import PeerConf  # noqa: E402
from wggui.remote import FakeRunner  # noqa: E402

SERVER_PUB = fake_key(0x10)
PEERS = [  # Name, key byte, address, endpoint, seconds since handshake, rx, tx
    ("laptop", 0x21, "10.98.0.2/32", "203.0.113.5:40312", 25, 48_000_000, 3_200_000),
    ("phone", 0x22, "10.98.0.3/32", "198.51.100.23:51002", 70, 9_400_000, 1_100_000),
    ("office", 0x23, "10.98.0.4/32, 192.0.2.0/24", "office.example.org:51820", 3_600 * 5, 120_000_000, 88_000_000),
    ("tablet", 0x24, "10.98.0.5/32", None, 0, 0, 0),
]


def conf_text() -> str:
    lines = ["[Interface]", "Address = 10.98.0.1/24", "ListenPort = 51820", f"PrivateKey = {REDACTED}", ""]
    for name, n, ips, _ep, _hs, _rx, _tx in PEERS:
        lines += [f"# {name}", "[Peer]", f"PublicKey = {fake_key(n)}", f"PresharedKey = {REDACTED}",
                  f"AllowedIPs = {ips}", ""]
    return "\n".join(lines)


def dump_text() -> str:
    now = int(time.time())
    out = [f"wg0\t{REDACTED}\t{SERVER_PUB}\t51820\toff"]
    for _name, n, ips, ep, hs, rx, tx in PEERS:
        out.append(f"wg0\t{fake_key(n)}\t{REDACTED}\t{ep or '(none)'}\t{ips.replace(' ', '')}\t"
                   f"{now - hs if hs else 0}\t{rx}\t{tx}\t{'25' if n == 0x22 else 'off'}")
    return "\n".join(out) + "\n"


def runner() -> FakeRunner:
    return (FakeRunner(label="demo")
            .on(r"os-release", 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n')
            .on(r"^id -u", "0\n")
            .on(r"command -v", "wg\nwg-quick\nsystemctl\n")
            .on(r"basename", "wg0\n")
            .on(r"wg pubkey", SERVER_PUB + "\n")
            .on(r"presharedkey.*wg0\.conf", conf_text())
            .on(r"wg show all dump", dump_text())
            .on(r"^systemctl list-units", "wg-quick@wg0.service loaded active exited WireGuard via wg-quick(8)\n"))



def main() -> None:
    # default: publish/docs/images (published as docs/images, see scripts/publish.py)
    out = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), ROOT / "publish" / "docs" / "images"))
    lang = sys.argv[sys.argv.index("--lang") + 1] if "--lang" in sys.argv else "en"
    out.mkdir(parents=True, exist_ok=True)
    os.environ["LANGUAGE"] = lang  # use the same language for standard buttons in the platform theme
    QLocale.setDefault(QLocale(lang))
    app = QApplication([])
    i18n.set_language(lang)
    install_qt_translation(app, QLocale(lang))
    QIcon.setThemeName("breeze")
    if "Breeze" in QStyleFactory.keys():  # noqa: SIM118 – keys() is a function, not a dict
        app.setStyle("Breeze")
    from wggui.ui.dialogs import ClientDialog, PlanDialog, QrDialog, SyncDialog
    from wggui.ui.main_window import MainWindow


    def shot(widget, name, size=None):
        if size:
            widget.resize(*size)
        widget.show()
        app.processEvents()
        widget.grab().save(str(out / f"{name}.png"))

    db = Database(":memory:")
    hid = db.add_host("vpn1", "root@vpn1.example.org")
    db.add_host("vpn2", "admin@vpn2.example.org", sudo=True)
    w = MainWindow(db, runner_factory=lambda host: runner(), confirm_import=lambda *a: True)
    w.resize(1280, 760)
    w.show()
    w.select(hid)
    w.import_from_server()
    while w.bg.busy:
        app.processEvents()
    w.refresh_all()
    while w.bg.busy:
        app.processEvents()
    w.select(hid, "wg0")
    w.table.selectRow(1)
    shot(w, "main-window")
    w.select(hid)
    shot(w, "host")
    nets = ["10.98.0.1/24"]
    d = ClientDialog("vpn1 / wg0", nets, ["10.98.0.2/32"], ["10.98.0.6/32"], "vpn1.example.org:51820", "10.98.0.1")
    d.name.setText("phone-2")
    shot(d, "add-client")
    base = conf_text()
    ops = [edit.PeerOp("edit", fake_key(0x22), "phone-anna", ("10.98.0.3/32",), 25)]
    plan = apply.WritePlan("vpn1", "wg0", "/etc/wireguard", edit.diff_lines(base, edit.transform_local(base, ops)), "x",
                           "c" * 64, True, apply.make_tag(), action=i18n._("Peer bearbeiten"))
    shot(PlanDialog(plan), "preview", (760, 560))
    pair = keys.generate_keypair()
    spec = client.ClientSpec("phone-2", ["10.98.0.6/32"], "vpn1.example.org:51820", False, ["10.98.0.1"])
    qr = QrDialog("phone-2", client.client_config(spec, pair.private, SERVER_PUB, keys.generate_psk(), nets))
    shot(qr, "qr-code")
    qr.saved_to = "-"
    drifts = sync.compute(db.peers(db.interfaces(hid)[0].id)[:3] + [], *_sync_input())
    shot(SyncDialog("vpn1 / wg0", drifts), "sync")
    from wggui.ui.provision_dialogs import NewInterfaceDialog, TunnelDialog, TunnelEnd

    shot(NewInterfaceDialog("vpn1", {"wg0": (["10.98.0.1/24"], 51820)}, systemd=True), "new-interface")
    own = TunnelEnd(hid, "vpn1", "wg0", SERVER_PUB, ("10.98.0.1/24",), "vpn1.example.org:51820")
    other = TunnelEnd(2, "vpn2", "wg0", fake_key(0x31), ("10.98.5.1/24",), "vpn2.example.org:51820")
    shot(TunnelDialog(own, [other]), "tunnel")
    # app icon next to the screenshots, so README previews work in publish/ and in the public root
    shutil.copy(ROOT / "packaging" / "flatpak" / "io.github.n8m4re.WGGUI.svg", out / "icon.svg")
    print(f"Screenshots: {out}")


def _sync_input():
    from wggui.parse import parse_conf

    conf = parse_conf(conf_text())
    conf.peers[0] = PeerConf(conf.peers[0].public_key, "laptop-work", conf.peers[0].allowed_ips, has_psk=True)
    return conf, None


if __name__ == "__main__":
    main()
