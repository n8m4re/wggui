"""Stage 2: run SSH per distro to test write plans, backups, rollback timers, and confirmation.

Interfaces are down in the container (kernel module), so this tests the file-only path. Real
`wg-quick strip` also checks the file for `wg syncconf`. The rollback timer must survive the
SSH session (setsid), including under `sudo -n` (Ubuntu).
"""

import time

import pytest

from wggui import apply, client, edit, keys
from wggui.apply import WritePlan
from wggui.discover import discover

from .test_targets import SEED

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh(target):
    target.run(["sh", "-c", SEED + "rm -f /etc/wireguard/wg0.conf.wggui-* /etc/wireguard/.wggui-*\n"])
    return target


def _plan(runner, name: str, seconds: int) -> tuple[WritePlan, str, str]:
    prepared = apply.prepare(runner, "wg0")
    pair, psk = keys.generate_keypair(), keys.generate_psk()
    block = client.server_peer_block(name, pair.public, ["10.97.0.9/32"], psk)
    plan = WritePlan("it", "wg0", "/etc/wireguard", client.preview_peer_block(name, pair.public, ["10.97.0.9/32"],
                     True), block, prepared.conf_hash, prepared.running, apply.make_tag(), rollback_seconds=seconds)
    apply.check_plan(plan, prepared)
    return plan, pair.public, psk


def _conf(runner) -> str:
    return runner.run(["cat", "/etc/wireguard/wg0.conf"]).stdout


def test_public_key_matches_wg(target):
    pair = keys.generate_keypair()  # disposable key for this comparison only
    assert target.run(["wg", "pubkey"], stdin=pair.private + "\n").stdout.strip() == pair.public


def test_prepare_on_target(fresh):
    prepared = apply.prepare(fresh, "wg0")
    assert prepared.setsid and prepared.wg_quick and not prepared.running


def test_write_and_confirm(fresh):
    before = _conf(fresh)
    plan, public, psk = _plan(fresh, "handy", seconds=3)
    assert apply.write_and_confirm(fresh, plan) == "file-only"
    after = _conf(fresh)
    assert after.startswith(before.rstrip("\n")) and public in after and f"PresharedKey = {psk}" in after
    assert fresh.run(["cat", plan.backup_path]).stdout == before
    mode = fresh.run(["stat", "-c", "%a", "/etc/wireguard/wg0.conf"]).stdout.strip()
    assert mode == "600"
    stripped = fresh.run(["wg-quick", "strip", "/etc/wireguard/wg0.conf"]).stdout  # real wg-quick checks syntax
    assert public in stripped
    time.sleep(4)  # timer expires but must do nothing
    assert _conf(fresh) == after
    state = discover(fresh)
    assert "handy" in [p.name for p in state.confs["wg0"].peers]


def test_rollback_survives_session_end(fresh):
    before = _conf(fresh)
    plan, public, _ = _plan(fresh, "laptop2", seconds=2)
    apply.apply(fresh, plan)  # without confirmation; SSH call has ended
    assert public in _conf(fresh)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _conf(fresh) != before:
        time.sleep(0.5)
    assert _conf(fresh) == before, "Rollback-Timer hat die Config nicht zurückgespielt"
    with pytest.raises(apply.NotConfirmedError):
        apply.write_and_confirm(_FailConfirm(fresh), _plan(fresh, "x", 2)[0])
    time.sleep(4)
    assert _conf(fresh) == before


def test_changed_file_is_refused(fresh):
    plan, public, _ = _plan(fresh, "handy", seconds=30)
    fresh.run(["sh", "-c", "echo '# von Hand' >> /etc/wireguard/wg0.conf"])
    with pytest.raises(Exception, match="geändert"):
        apply.apply(fresh, plan)
    assert public not in _conf(fresh)


class _FailConfirm:
    """Allow APPLY, then lose the confirmation as with a dropped connection."""

    def __init__(self, inner):
        self.inner, self.label = inner, inner.label

    def run(self, argv, *, stdin=None, check=True):
        if "confirmed" in " ".join(argv):
            argv = ["sh", "-c", "exit 255"]
        return self.inner.run(argv, stdin=stdin, check=check)

    def remote_addresses(self):
        return []


# --- Stage 3: edit peers (host awk: mawk, gawk, BusyBox) ---------------------------------------------


def _edit_plan(runner, ops, seconds=30) -> WritePlan:
    prepared = apply.prepare(runner, "wg0")
    plan = WritePlan("it", "wg0", "/etc/wireguard", "diff", edit.order_text(ops), prepared.conf_hash,
                     prepared.running, apply.make_tag(), rollback_seconds=seconds, builder=apply.BUILD_EDIT,
                     builder_args=(edit.EDIT_AWK,), action="Peers ändern")
    apply.check_plan(plan, prepared)
    return plan


def test_edit_on_host_matches_local_preview(fresh):
    state = discover(fresh)
    before_redacted = state.conf_texts["wg0"]
    laptop, phone = (p.public_key for p in state.confs["wg0"].peers)
    ops = [edit.PeerOp("edit", phone, "handy-neu", ("10.97.0.3/32",), 25),
           edit.PeerOp("state", laptop, state="off")]
    expected = edit.transform_local(before_redacted, ops)  # local preview on the redacted copy
    psk_line = next(ln for ln in _conf(fresh).splitlines() if ln.startswith("PresharedKey"))

    apply.write_and_confirm(fresh, _edit_plan(fresh, ops))

    after = discover(fresh)
    assert after.conf_texts["wg0"] == expected  # host awk matches local awk byte for byte after redaction
    peers = {p.public_key: p for p in after.confs["wg0"].peers}
    assert peers[laptop].disabled and peers[phone].name == "handy-neu" and peers[phone].has_psk
    assert psk_line in _conf(fresh)  # PSK remains on the host
    assert "PresharedKey = (redacted)" in after.conf_texts["wg0"]
    stripped = fresh.run(["wg-quick", "strip", "/etc/wireguard/wg0.conf"]).stdout
    active = "\n".join(ln for ln in stripped.splitlines() if not ln.lstrip().startswith("#"))
    assert laptop not in active and phone in active  # disabled peer is only a comment; wg(8) ignores it


def test_remove_and_missing_peer(fresh):
    state = discover(fresh)
    laptop = state.confs["wg0"].peers[0].public_key
    apply.write_and_confirm(fresh, _edit_plan(fresh, [edit.PeerOp("remove", laptop)]))
    assert laptop not in _conf(fresh)
    before = _conf(fresh)
    with pytest.raises(Exception, match="nicht gefunden|Peer nicht"):
        apply.apply(fresh, _edit_plan(fresh, [edit.PeerOp("remove", laptop)]))
    assert _conf(fresh) == before  # unchanged; no backup rotation


# --- Running tunnel: wg syncconf updates runtime (kernel or wireguard-go in container) -----------------


@pytest.fixture
def running(fresh):  # noqa: F811
    if not fresh.run(["wg-quick", "up", "wg0"], check=False).ok:
        pytest.skip("wg-quick up geht im Container nicht (weder Kernelmodul noch wireguard-go)")
    yield fresh
    fresh.run(["wg-quick", "down", "wg0"], check=False)


def _runtime_peers(runner) -> set[str]:
    return {p for p in runner.run(["wg", "show", "wg0", "peers"]).stdout.split()}


def test_running_tunnel_follows_changes(running):
    before = _runtime_peers(running)
    assert len(before) == 2
    plan, public, _psk = _plan(running, "handy", seconds=30)
    assert plan.running
    assert apply.write_and_confirm(running, plan) == "synced"
    assert _runtime_peers(running) == before | {public}  # new peer active immediately; existing peers remain

    apply.write_and_confirm(running, _edit_plan(running, [edit.PeerOp("state", public, state="off")]))
    assert _runtime_peers(running) == before  # disabled peer removed from runtime


def test_running_tunnel_rollback_restores_runtime(running):
    before = _runtime_peers(running)
    plan, public, _psk = _plan(running, "laptop3", seconds=2)
    assert apply.apply(running, plan) == "synced"  # without confirmation
    assert public in _runtime_peers(running)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _runtime_peers(running) != before:
        time.sleep(0.5)
    assert _runtime_peers(running) == before, "Rollback hat die Laufzeit nicht zurückgesetzt"
