"""Stage 3: run the host awk program locally against the config fixture."""

import subprocess

import pytest

from tests.conftest import fixture_text, redact_conf
from tests.keys import K1, K2, K3, K4, K9, PSK, SECRETS
from wggui.apply import BUILD_EDIT
from wggui.edit import EDIT_AWK, PeerOp, diff_lines, order_text, transform_local
from wggui.parse import parse_conf

CONF = fixture_text("wg0.conf")  # unredacted, as on the host


def _peers(text: str) -> dict:
    return {p.public_key: p for p in parse_conf(text).peers}


def test_edit_name_allowed_keepalive_keeps_psk():
    new = transform_local(CONF, [PeerOp("edit", K2, "handy-neu", ("10.98.0.3/32",), 25)])
    peer = _peers(new)[K2]
    assert (peer.name, peer.allowed_ips, peer.keepalive, peer.has_psk) == ("handy-neu", ["10.98.0.3/32"], 25, True)
    assert PSK in new  # PSK remains unchanged on the host
    assert "### begin phone ###" not in new and "### end phone ###" not in new  # old name and PiVPN markers removed
    others = {k: p for k, p in _peers(new).items() if k != K2}
    assert others == {k: p for k, p in _peers(CONF).items() if k != K2}  # rest unchanged


def test_edit_without_name_keeps_comment_and_removes_keepalive():
    new = transform_local(CONF, [PeerOp("edit", K3, "", ("10.98.0.4/32",), None)])
    peer = _peers(new)[K3]
    assert peer.name == "office" and peer.keepalive is None and peer.endpoint == "office.example.org:51820"


def test_edit_lowercase_and_inline_comment():
    new = transform_local(CONF, [PeerOp("edit", K4, "", ("10.98.0.5/32", "10.98.1.0/24"))])
    assert _peers(new)[K4].allowed_ips == ["10.98.0.5/32", "10.98.1.0/24"]
    assert "# Inline-Kommentar" not in new


def test_disable_and_enable_roundtrip():
    off = transform_local(CONF, [PeerOp("state", K2, state="off")])
    peer = _peers(off)[K2]
    assert peer.disabled and peer.name == "phone" and peer.has_psk
    stripped = [ln for ln in off.splitlines() if not ln.lstrip().startswith("#")]
    assert K2 not in "\n".join(stripped)  # wg-quick no longer sees the peer
    on = transform_local(off, [PeerOp("state", K2, state="on")])
    assert on == CONF


def test_edit_disabled_peer_stays_disabled():
    off = transform_local(CONF, [PeerOp("state", K1, state="off")])
    new = transform_local(off, [PeerOp("edit", K1, "laptop2", ("10.98.0.2/32",))])
    peer = _peers(new)[K1]
    assert peer.disabled and peer.name == "laptop2"


def test_remove_takes_lead_comment_and_pivpn_block():
    new = transform_local(CONF, [PeerOp("remove", K2)])
    assert K2 not in new and "phone" not in new and PSK not in new
    assert set(_peers(new)) == set(_peers(CONF)) - {K2}
    assert "PrivateKey" in new and "PostUp" in new  # [Interface] remains


def test_add_and_multiple_ops():
    new = transform_local(CONF, [PeerOp("add", K9, "neu", ("10.98.0.9/32",), 25), PeerOp("remove", K1),
                                 PeerOp("state", K3, state="off")])
    peers = _peers(new)
    assert K1 not in peers and peers[K9].name == "neu" and peers[K3].disabled


@pytest.mark.parametrize(("ops", "text"), [
    ([PeerOp("remove", "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")], "nicht gefunden"),
    ([PeerOp("add", K1, "x", ("10.98.0.9/32",))], "existiert schon"),
])
def test_errors(ops, text):
    with pytest.raises(ValueError, match=text):
        transform_local(CONF, ops)


@pytest.mark.parametrize("op", [
    PeerOp("edit", "kein-key", "x", ("10.98.0.2/32",)),
    PeerOp("edit", K1, "a\tb", ("10.98.0.2/32",)),
    PeerOp("edit", K1, "x", ()),
    PeerOp("edit", K1, "x", ("kein-netz",)),
    PeerOp("state", K1),
    PeerOp("drop", K1),
    PeerOp("edit", K1, "x", ("10.98.0.2/32",), 0),
])
def test_invalid_orders(op):
    with pytest.raises(ValueError):
        order_text([op])


def test_one_op_per_peer():
    with pytest.raises(ValueError, match="nur eine"):
        order_text([PeerOp("remove", K1), PeerOp("state", K1, state="off")])


def test_empty_order_changes_nothing():
    """An empty operations file must not be read as the config (FILENAME versus NR == FNR)."""
    assert transform_local(CONF, []) == CONF


def test_build_edit_reads_order_from_stdin(tmp_path):
    """As on the host: operations on stdin, config in a file."""
    conf = tmp_path / "wg0.conf"
    conf.write_text(CONF)
    out = subprocess.run(["sh", "-c", BUILD_EDIT, "sh", str(conf), EDIT_AWK],
                         input=order_text([PeerOp("remove", K1)]), capture_output=True, text=True, check=True).stdout
    assert out == transform_local(CONF, [PeerOp("remove", K1)])


def test_preview_on_redacted_copy_matches_host_result():
    """Preview runs the same program on the redacted copy; diff contains no secrets."""
    ops = [PeerOp("edit", K2, "handy-neu", ("10.98.0.3/32",), 25)]
    redacted = redact_conf(CONF)
    preview = diff_lines(redacted, transform_local(redacted, ops))
    assert "+# handy-neu" in preview and "-### begin phone ###" in preview
    for secret in SECRETS:
        assert secret not in preview
    assert redact_conf(transform_local(CONF, ops)) == transform_local(redacted, ops)


def test_redaction_of_disabled_and_hook_keys():
    off = transform_local(CONF, [PeerOp("state", K2, state="off")])
    hook = off.replace("PostUp = iptables", f"PostUp = wg set %i peer {K9} preshared-key <(echo {PSK}) ; iptables")
    out = redact_conf(hook)
    for secret in SECRETS:
        assert secret not in out
    assert "#wggui-off PresharedKey = (redacted)" in out
    assert parse_conf(out).peers[1].disabled
