"""Reconcile app, server file, and runtime without Qt or SSH."""

from tests.keys import K1, K2, K3, K9
from wggui import sync
from wggui.db import Peer
from wggui.parse import InterfaceConf, PeerConf, RuntimeInterface, RuntimePeer


def _app(key, name, ips, **kw) -> Peer:
    return Peer(0, 1, name, key, ips, kw.get("endpoint"), kw.get("keepalive"), kw.get("has_psk", False),
                kw.get("extra", []), kw.get("disabled", False))


def _rt(*keys, allowed: dict | None = None) -> RuntimeInterface:
    """Runtime matches the file (AllowedIPs/Keepalive from CONF) unless `allowed` overrides it."""
    file = {p.public_key: p for p in CONF.peers}
    peers = {}
    for k in keys:
        ips = (allowed or {}).get(k, file[k].allowed_ips if k in file else [])
        peers[k] = RuntimePeer(k, None, ips, 0, 0, 0, file[k].keepalive if k in file else None)
    return RuntimeInterface("wg0", public_key=None, listen_port=51820, peers=peers)


CONF = InterfaceConf(addresses=["10.98.0.1/24"], peers=[
    PeerConf(K1, "laptop", ["10.98.0.2/32"]),
    PeerConf(K2, "phone", ["10.98.0.3/32"], has_psk=True),
    PeerConf(K3, "office", ["10.98.0.4/32"], disabled=True),
])


def test_no_drift():
    app = [_app(K1, "laptop", ["10.98.0.2/32"]), _app(K2, "phone", ["10.98.0.3/32"], has_psk=True),
           _app(K3, "office", ["10.98.0.4/32"], disabled=True)]
    assert sync.compute(app, CONF, _rt(K1, K2)) == []  # disabled K3 is correctly absent from runtime


def test_drift_kinds_and_defaults():
    app = [_app(K1, "laptop-neu", ["10.98.0.2/32"]), _app(K2, "phone", ["10.98.0.3/32"], has_psk=False),
           _app(K9, "alt", ["10.98.0.9/32"])]
    drifts = {d.key: d for d in sync.compute(app, CONF, _rt(K1, K9))}
    assert drifts[K1].kind == "unterschied" and sync.TO_SERVER in drifts[K1].choices
    assert drifts[K2].choices == (sync.TO_APP, sync.IGNORE)  # only PSK differs, so cannot write
    assert drifts[K3].kind == "nur_server" and drifts[K3].default == sync.TO_APP
    assert drifts[K9].kind == "nur_app" and drifts[K9].default == sync.REMOVE_APP
    runtime = drifts[None]
    assert runtime.default == sync.IGNORE and "alt" in runtime.detail and "phone" in runtime.detail


def test_resolve_directions():
    app = [_app(K1, "laptop-neu", ["10.98.0.2/32"], keepalive=25, disabled=True), _app(K9, "alt", ["10.98.0.9/32"])]
    drifts = sync.compute(app, CONF, _rt(K1, K2, K9))
    pick = {}
    for i, d in enumerate(drifts):
        pick[i] = {K1: sync.TO_SERVER, K9: sync.ADD_SERVER, K2: sync.REMOVE_SERVER, K3: sync.TO_APP,
                   None: sync.SYNC_RUNTIME}[d.key]
    res = sync.resolve(drifts, pick, app, CONF)
    ops = {o.public_key: o for o in res.ops}
    assert ops[K1].op == "edit" and ops[K1].name == "laptop-neu" and ops[K1].state == "off" and ops[K1].keepalive == 25
    assert ops[K9].op == "add" and ops[K9].allowed_ips == ("10.98.0.9/32",)
    assert ops[K2].op == "remove"
    assert [p.public_key for p in res.db_upserts] == [K3] and res.sync_runtime and res.writes_server


def test_resolve_rejects_unknown_choice():
    drifts = sync.compute([], CONF, None)
    try:
        sync.resolve(drifts, {0: sync.ADD_SERVER}, [], CONF)
    except ValueError:
        return
    raise AssertionError("ungültige Auswahl nicht erkannt")


def test_runtime_allowed_ips_differ():
    app = [_app(K1, "laptop", ["10.98.0.2/32"]), _app(K2, "phone", ["10.98.0.3/32"], has_psk=True),
           _app(K3, "office", ["10.98.0.4/32"], disabled=True)]
    [drift] = sync.compute(app, CONF, _rt(K1, K2, allowed={K2: ["10.98.0.3/32", "10.98.5.0/24"]}))
    assert drift.kind == "laufzeit" and "phone (AllowedIPs)" in drift.detail
