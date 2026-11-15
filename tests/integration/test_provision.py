"""Stage 4 on real distributions: create/start/confirm/roll back an interface, and a real tunnel between two
containers (handshake + ping through the tunnel). Kernel module or wireguard-go inside the container only."""

import time

import pytest

from wggui import apply, keys, provision
from wggui.discover import check_connection, discover
from wggui.provision import NewInterface, TunnelSide

from .conftest import SELECTED, TARGETS, make_runner

pytestmark = pytest.mark.integration


def _cleanup(runner, name):
    runner.run(["sh", "-c", 'wg-quick down "/etc/wireguard/$1.conf" >/dev/null 2>&1; '
                            'rm -f "/etc/wireguard/$1.conf" /etc/wireguard/.wggui-pending-"$1"-*', "sh", name],
               check=False)


def _up(runner, name) -> bool:
    return runner.run(["wg", "show", name], check=False).ok


def test_create_confirm_and_rollback(target):
    _cleanup(target, "wg7")
    new = NewInterface("it", "wg7", "10.97.7.1/24", 51827, start=True, enable=False, rollback_seconds=30)
    try:
        public = provision.create_interface(target, new)
    except ValueError as err:
        if "starten" in str(err):
            pytest.skip("wg-quick up not possible in this container")
        raise
    provision.confirm_interface(target, new)
    assert _up(target, "wg7")
    state = discover(target)
    assert state.runtime["wg7"].public_key == public and state.confs["wg7"].addresses == ["10.97.7.1/24"]
    assert "(redacted)" in state.conf_texts["wg7"]  # private key read only redacted
    _cleanup(target, "wg7")

    quick = NewInterface("it", "wg8", "10.97.8.1/24", 51828, start=True, enable=False, rollback_seconds=2)
    _cleanup(target, "wg8")
    provision.create_interface(target, quick)  # not confirmed → timer removes it
    deadline = time.monotonic() + 15  # timer stops the interface, then removes the file
    while time.monotonic() < deadline and (_up(target, "wg8") or
                                           target.run(["test", "-e", "/etc/wireguard/wg8.conf"], check=False).ok):
        time.sleep(0.5)
    assert not _up(target, "wg8")
    assert not target.run(["test", "-e", "/etc/wireguard/wg8.conf"], check=False).ok
    _cleanup(target, "wg8")


@pytest.mark.skipif(not {"debian", "alpine"} <= set(SELECTED), reason="needs the debian and alpine targets")
def test_tunnel_between_two_servers(stack):
    a, b = make_runner(TARGETS["debian"]), make_runner(TARGETS["alpine"])
    for r in (a, b):
        _cleanup(r, "wg9")
    new_a = NewInterface("debian", "wg9", "10.97.9.1/24", 51829, enable=False)
    new_b = NewInterface("alpine", "wg9", "10.97.9.2/24", 51829, enable=False)
    try:
        pub_a = provision.create_interface(a, new_a)
        pub_b = provision.create_interface(b, new_b)
    except ValueError as err:
        pytest.skip(f"interface cannot be started in the containers: {err}")
    provision.confirm_interface(a, new_a)
    provision.confirm_interface(b, new_b)
    try:
        # compose service names resolve inside the compose network
        side_a = TunnelSide("debian", "wg9", pub_a, "debian:51829", ("10.97.9.1/32",))
        side_b = TunnelSide("alpine", "wg9", pub_b, "alpine:51829", ("10.97.9.2/32",))
        prep_a, prep_b = apply.prepare(a, "wg9"), apply.prepare(b, "wg9")
        plan_a, plan_b = provision.tunnel_plans(side_a, side_b, keys.generate_psk(),
                                                (prep_a.conf_hash, prep_b.conf_hash), (prep_a.running, prep_b.running))
        assert apply.write_and_confirm(a, plan_a) == "synced"
        assert apply.write_and_confirm(b, plan_b) == "synced"
        assert b.run(["ping", "-c", "3", "-W", "2", "10.97.9.1"], check=False).ok  # traffic through the tunnel
        dump = a.run(["wg", "show", "wg9", "latest-handshakes"]).stdout.split()
        assert dump[0] == pub_b and int(dump[1]) > 0  # real handshake
    finally:
        for r in (a, b):
            _cleanup(r, "wg9")


def test_install_tools_on_alpine(stack):
    if "alpine" not in SELECTED:
        pytest.skip("alpine target not selected")
    r = make_runner(TARGETS["alpine"])
    info = check_connection(r)
    assert info.family == "alpine"
    # wireguard-go depends on wireguard-tools-wg, so it has to go too (and comes back afterwards)
    removed = r.run(["apk", "del", "wireguard-go", "wireguard-tools", "wireguard-tools-wg",
                     "wireguard-tools-wg-quick"], check=False)
    if not removed.ok:
        pytest.skip(f"apk del failed: {removed.stderr[:200]}")
    try:
        assert not check_connection(r).wg_installed
        try:
            provision.install_tools(r, "alpine")
        except Exception as err:  # noqa: BLE001 – no package mirror reachable from the container
            pytest.skip(f"no package mirror reachable: {err}")
        assert check_connection(r).wg_installed
    finally:
        r.run(["apk", "add", "--no-cache", "wireguard-tools", "wireguard-go"], check=False)
