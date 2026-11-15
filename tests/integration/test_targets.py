"""Run SSH against Debian, Ubuntu (sudo), Fedora, and Alpine: discovery, host-side redaction,
and database import. Each test runs per distro (`distro` fixture in conftest.py).

Keys are generated in the container (wg genkey/genpsk) and read here only for verification.
"""


import pytest

from wggui.db import Database
from wggui.discover import discover
from wggui.remote import Result

from .conftest import make_runner

pytestmark = pytest.mark.integration

# In the test container only: create a hand-maintained wg0.conf, as on an established server.
# Include a commented-out old key that must also be redacted.
SEED = r"""set -e
umask 077
old=$(wg genkey)
priv=$(wg genkey); p1=$(wg genkey | wg pubkey); p2=$(wg genkey | wg pubkey); psk=$(wg genpsk)
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = 10.97.0.1/24
ListenPort = 51820
PrivateKey = $priv
# PrivateKey = $old

# laptop
[Peer]
PublicKey = $p1
AllowedIPs = 10.97.0.2/32

### begin phone ###
[Peer]
PublicKey = $p2
PresharedKey = $psk
AllowedIPs = 10.97.0.3/32, 192.0.2.0/24
### end phone ###
EOF
"""


class Recording:
    """Record all output returned over SSH."""

    def __init__(self, inner):
        self.inner, self.label, self.outputs = inner, inner.label, []

    def run(self, argv, *, stdin=None, check=True) -> Result:
        res = self.inner.run(argv, stdin=stdin, check=check)
        self.outputs.append(res.stdout + res.stderr)
        return res


@pytest.fixture(scope="module")
def seeded(target):
    target.run(["sh", "-c", SEED])
    secrets = {
        "private": target.run(["sh", "-c", "sed -n 's/^PrivateKey = //p' /etc/wireguard/wg0.conf"]).stdout.strip(),
        "psk": target.run(["sh", "-c", "sed -n 's/^PresharedKey = //p' /etc/wireguard/wg0.conf"]).stdout.strip(),
        "old": target.run(["sh", "-c", "sed -n 's/^# PrivateKey = //p' /etc/wireguard/wg0.conf"]).stdout.strip(),
    }
    expected_pub = target.run(["sh", "-c", "sed -n 's/^PrivateKey = //p' /etc/wireguard/wg0.conf | wg pubkey"])
    assert all(secrets.values())
    return secrets, expected_pub.stdout.strip()


def _assert_no_secrets(text: str, secrets: dict) -> None:
    for name, value in secrets.items():
        assert value not in text, f"{name} wurde übertragen"


def test_discover_over_ssh(distro, target, seeded):
    secrets, expected_pub = seeded
    rec = Recording(target)
    state = discover(rec)

    assert state.info.os_name.startswith(distro.os_prefix)
    assert state.info.family == distro.family
    assert state.info.is_root and state.info.wg_installed and state.info.wg_quick
    conf = state.confs["wg0"]
    assert [p.name for p in conf.peers] == ["laptop", "phone"]
    assert conf.peers[1].has_psk and conf.peers[1].allowed_ips == ["10.97.0.3/32", "192.0.2.0/24"]
    assert state.public_keys["wg0"] == expected_pub
    _assert_no_secrets("\n".join(rec.outputs), secrets)
    _assert_no_secrets(repr(state), secrets)


def test_runtime_with_interface_up(target, seeded):
    # use the host kernel module if loaded, or wireguard-go in the container; never load modules ourselves
    secrets, expected_pub = seeded
    if not target.run(["wg-quick", "up", "wg0"], check=False).ok:
        pytest.skip("wg-quick up geht im Container nicht (weder Kernelmodul noch wireguard-go)")
    try:
        rec = Recording(target)
        state = discover(rec)
        rt = state.runtime["wg0"]
        assert rt.public_key == expected_pub and rt.listen_port == 51820
        assert len(rt.peers) == 2
        assert all(p.latest_handshake == 0 for p in rt.peers.values())
        assert not any("läuft nicht" in w for w in state.warnings)
        _assert_no_secrets("\n".join(rec.outputs), secrets)
    finally:
        target.run(["wg-quick", "down", "wg0"], check=False)


def test_import_into_database(target, seeded, tmp_path):
    secrets, expected_pub = seeded
    db = Database(tmp_path / "it.db")
    hid = db.add_host("it", "root@127.0.0.1", 22211)
    state = discover(target)
    db.apply_import(hid, state)
    [iface] = db.interfaces(hid)
    assert iface.public_key == expected_pub
    assert [p.name for p in db.peers(iface.id)] == ["laptop", "phone"]
    assert db.plan_import(hid, discover(target)) == []
    _assert_no_secrets("\n".join(db.conn.iterdump()), secrets)
    db.close()


def test_without_sudo_no_access(distro, seeded):
    """Non-root login without sudo: clear warning, no data read."""
    if not distro.sudo:
        pytest.skip("Ziel nutzt root-Login")
    state = discover(make_runner(distro, sudo=False))
    assert not state.info.is_root and state.confs == {}
    assert "sudo" in state.warnings[0]
