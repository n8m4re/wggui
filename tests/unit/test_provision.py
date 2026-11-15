"""Stage 4: new interface (host script run locally with fake wg/wg-quick), install command, tunnel blocks."""

import shutil
import subprocess
import time

import pytest

from tests.keys import K1, K2, SECRETS
from wggui import provision
from wggui.apply import CONFIRM
from wggui.parse import parse_conf
from wggui.provision import CREATE_IFACE, ROLLBACK_CREATE, NewInterface, TunnelSide
from wggui.remote import FakeRunner

PRIV = "aG9zdHByaXZhdGVrZXlob3N0cHJpdmF0ZWtleTEyMzQ="  # fake "host private key" from the fake wg genkey
FAKE_WG = f"""#!/bin/sh
case "$1" in
  genkey) echo "{PRIV}" ;;
  pubkey) read k; echo "{K1}" ;;
  *) exit 2 ;;
esac
"""
# Never reach the real systemctl of the workstation: a fake records enable/disable
FAKE_SYSTEMCTL = """#!/bin/sh
case "$1" in
  enable) [ "${FAKE_UP_FAIL:-0}" = 1 ] && exit 1; touch "$FAKE_LOG/up"; echo "$@" >> "$FAKE_LOG/systemctl" ;;
  disable) rm -f "$FAKE_LOG/up"; echo "$@" >> "$FAKE_LOG/systemctl" ;;
  *) exit 0 ;;
esac
"""
FAKE_WG_QUICK = """#!/bin/sh
case "$1" in
  up) [ "${FAKE_UP_FAIL:-0}" = 1 ] && exit 1; touch "$FAKE_LOG/up" ;;
  down) rm -f "$FAKE_LOG/up" ;;
esac
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("wg", FAKE_WG), ("wg-quick", FAKE_WG_QUICK), ("systemctl", FAKE_SYSTEMCTL)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")  # fakes first – the real systemctl is never reached
    monkeypatch.setenv("FAKE_LOG", str(tmp_path))
    conf_dir = tmp_path / "wireguard"
    return conf_dir, tmp_path


def _create(conf_dir, seconds="30", start="1", enable="0", check=True):
    return subprocess.run(["sh", "-c", CREATE_IFACE, "sh", "wg1", str(conf_dir), "10.98.1.1/24", "51821",
                           "20261115-120000", seconds, ROLLBACK_CREATE, start, enable],
                          capture_output=True, text=True, check=check)


@pytest.mark.skipif(shutil.which("setsid") is None, reason="setsid missing")
def test_create_interface_keeps_key_on_host(host):
    conf_dir, log = host
    out = _create(conf_dir).stdout
    assert f"pubkey {K1}" in out and "started" in out and PRIV not in out  # only the public key comes back
    conf = (conf_dir / "wg1.conf").read_text()
    assert f"PrivateKey = {PRIV}" in conf and "ListenPort = 51821" in conf
    assert oct((conf_dir / "wg1.conf").stat().st_mode & 0o777) == "0o600"
    assert (log / "up").exists()
    assert subprocess.run(["sh", "-c", CONFIRM, "sh", "wg1", str(conf_dir), "20261115-120000"],
                          capture_output=True, text=True).returncode == 0
    assert _create(conf_dir, check=False).returncode == 3  # exists → refused, nothing overwritten


@pytest.mark.skipif(shutil.which("setsid") is None, reason="setsid missing")
def test_create_interface_rolls_back_without_confirmation(host):
    conf_dir, log = host
    _create(conf_dir, seconds="1")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and (conf_dir / "wg1.conf").exists():
        time.sleep(0.2)
    assert not (conf_dir / "wg1.conf").exists() and not (log / "up").exists()


def test_start_failure_removes_file(host, monkeypatch):
    conf_dir, _log = host
    monkeypatch.setenv("FAKE_UP_FAIL", "1")
    assert _create(conf_dir, check=False).returncode == 4
    assert not (conf_dir / "wg1.conf").exists()


def test_create_interface_api_hides_private_key():
    runner = FakeRunner().on(r"genkey", f"started\npubkey {K1}\n")
    new = NewInterface("vpn1", "wg1", "10.98.1.1/24", 51821)
    assert provision.create_interface(runner, new) == K1
    [call] = runner.calls
    assert call.stdin is None and not any(s in " ".join(call.argv) for s in SECRETS)
    assert "verlässt ihn nie" in new.preview and "PrivateKey" in new.preview and new.conf_path.endswith("wg1.conf")
    failing = FakeRunner().on(r"genkey", "", 3, "interface exists")
    with pytest.raises(ValueError, match="existiert schon"):
        provision.create_interface(failing, new)


def test_suggest_and_validate_new_interface():
    existing = {"wg0": (["10.98.0.1/24"], 51820), "wg1": (["10.98.1.1/24"], 51821)}
    name, address, port = provision.suggest_new_interface(existing)
    assert (name, address, port) == ("wg2", "10.98.2.1/24", 51822)
    provision.validate_new_interface(name, address, port, existing)
    for args, text in ((("wg0", "10.98.5.1/24", 51830), "gibt es"), (("wg5", "10.98.0.9/24", 51830), "überschneidet"),
                       (("wg5", "10.98.5.1/24", 51820), "nutzt schon"), (("wg5", "10.98.5.1", 51830), "Netzmaske"),
                       (("bad name!", "10.98.5.1/24", 51830), "Name")):
        with pytest.raises(ValueError, match=text):
            provision.validate_new_interface(*args, existing)


def test_install_command_per_family():
    assert provision.install_command("rhel") == ["dnf", "-y", "install", "wireguard-tools"]
    assert provision.install_command("alpine")[:2] == ["apk", "add"]
    assert provision.install_command("gentoo") is None
    runner = FakeRunner().on(r"apk add", "").on(r"command -v wg", "")
    provision.install_tools(runner, "alpine")
    with pytest.raises(ValueError):
        provision.install_tools(FakeRunner(), "gentoo")


def test_tunnel_plans_are_symmetric_and_hide_psk():
    a = TunnelSide("vpn1", "wg0", K1, "vpn1.example.org:51820", ("10.98.0.1/32",))
    b = TunnelSide("vpn2", "wg0", K2, "vpn2.example.org:51820", ("10.98.0.2/32", "192.0.2.0/24"))
    psk = "cHNrcHNrcHNrcHNrcHNrcHNrcHNrcHNrcHNrcHNrcHM="
    plan_a, plan_b = provision.tunnel_plans(a, b, psk, ("a" * 64, "b" * 64), (True, False))
    peer_on_a = parse_conf("[Interface]\n" + plan_a.block).peers[0]
    peer_on_b = parse_conf("[Interface]\n" + plan_b.block).peers[0]
    assert peer_on_a.public_key == K2 and peer_on_a.allowed_ips == ["10.98.0.2/32", "192.0.2.0/24"]
    assert peer_on_b.public_key == K1 and peer_on_b.endpoint == "vpn1.example.org:51820"
    assert peer_on_a.has_psk and peer_on_b.has_psk and peer_on_a.keepalive == 25
    assert psk not in plan_a.preview and psk not in repr(plan_a)
    assert (plan_a.host_name, plan_b.host_name, plan_a.running, plan_b.running) == ("vpn1", "vpn2", True, False)


@pytest.mark.skipif(shutil.which("setsid") is None, reason="setsid missing")
def test_enable_uses_systemd_unit(host):
    conf_dir, log = host
    out = _create(conf_dir, enable="1").stdout
    assert "enabled" in out and "enable --now wg-quick@wg1" in (log / "systemctl").read_text()
