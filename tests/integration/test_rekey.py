"""Rekey on real distros: host awk matches local preview; old PSK removed, new PSK added."""

import pytest

from wggui import apply, edit, keys
from wggui.discover import discover

from .test_write import _conf, _edit_plan, fresh  # noqa: F401 – Fixture

pytestmark = pytest.mark.integration


def test_rekey_on_host(fresh):  # noqa: F811
    state = discover(fresh)
    phone = state.confs["wg0"].peers[1]
    old_psk = next(ln for ln in _conf(fresh).splitlines() if ln.startswith("PresharedKey"))
    pair, psk = keys.generate_keypair(), keys.generate_psk()
    ops = [edit.PeerOp("rekey", phone.public_key, new_key=pair.public, psk=psk)]
    expected = edit.transform_local(state.conf_texts["wg0"], ops)

    apply.write_and_confirm(fresh, _edit_plan(fresh, ops))

    after = discover(fresh)
    assert after.conf_texts["wg0"] == expected
    text = _conf(fresh)
    assert phone.public_key not in text and pair.public in text
    assert old_psk not in text and f"PresharedKey = {psk}" in text
    peer = next(p for p in after.confs["wg0"].peers if p.public_key == pair.public)
    assert peer.name == "phone" and peer.allowed_ips == phone.allowed_ips
