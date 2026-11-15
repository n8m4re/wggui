import sqlite3

import pytest

from tests.conftest import debian_runner, fixture_text
from tests.keys import K1, K2, K3, K4, PUB0, SECRETS
from wggui.db import SCHEMA_VERSION, Database, default_db_path
from wggui.discover import discover


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def test_default_path(monkeypatch, tmp_path):
    monkeypatch.delenv("WGGUI_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_db_path() == tmp_path / "wireguard-gui" / "wggui.db"
    monkeypatch.setenv("WGGUI_DB", "/x/y.db")
    assert str(default_db_path()) == "/x/y.db"


def test_hosts_crud(db):
    hid = db.add_host("srv-a", "root@vpn.example.org", 2222)
    assert db.host(hid).port == 2222
    db.update_host(hid, "srv-b", "vpn-alias", None)
    assert (db.host(hid).name, db.host(hid).ssh_target, db.host(hid).port) == ("srv-b", "vpn-alias", None)
    db.set_host_os(hid, "Debian")
    assert db.host(hid).os == "Debian" and db.host(hid).checked_at
    db.delete_host(hid)
    assert db.hosts() == []


@pytest.mark.parametrize("name, target, port", [
    ("", "vpn", None), ("a b", "vpn", None), ("ok", "-oProxyCommand=x", None), ("ok", "vpn", 0),
])
def test_host_validation(db, name, target, port):
    with pytest.raises(ValueError):
        db.add_host(name, target, port)


def test_duplicate_host_name(db):
    db.add_host("srv-a", "vpn")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_host("srv-a", "vpn2")


def test_import_roundtrip(db):
    hid = db.add_host("srv-a", "vpn")
    state = discover(debian_runner())
    changes = db.plan_import(hid, state)
    assert changes[0].kind == "neu" and changes[0].subject == "Interface wg0"
    assert sum(c.subject.startswith("Peer") for c in changes) == 4
    db.apply_import(hid, state)

    [iface] = db.interfaces(hid)
    assert iface.public_key == PUB0 and iface.addresses == ["10.98.0.1/24"] and iface.listen_port == 51820
    assert iface.extra == [("PostUp", "iptables -A FORWARD -i %i -j ACCEPT"), ("MTU", "1420")]
    peers = {p.public_key: p for p in db.peers(iface.id)}
    assert peers[K1].name == "laptop" and peers[K2].has_psk
    assert db.plan_import(hid, state) == []  # second run: nothing to do


def test_import_detects_changes_and_keeps_app_names(db):
    hid = db.add_host("srv-a", "vpn")
    db.apply_import(hid, discover(debian_runner()))
    iface = db.interfaces(hid)[0]
    db.conn.execute("UPDATE peer SET name = 'drucker' WHERE public_key = ?", (K4,))  # name exists only in app

    text = fixture_text("wg0.conf").replace("10.98.0.4/32", "10.98.0.40/32")
    text = text.replace("# laptop\n", "# notebook\n")
    text = text[:text.index("### begin phone")]  # phone, office, and K4 removed
    state = discover(debian_runner(conf=text))
    changes = {(c.kind, c.subject): c.detail for c in db.plan_import(hid, state)}
    assert ("geändert", "Peer notebook (wg0)") in changes
    assert ("entfernt", "Peer phone (wg0)") in changes
    assert ("entfernt", "Peer drucker (wg0)") in changes

    db.apply_import(hid, state)
    assert [p.public_key for p in db.peers(iface.id)] == [K1]

    # peer without a config comment keeps its app name
    db.apply_import(hid, discover(debian_runner()))
    db.conn.execute("UPDATE peer SET name = 'drucker' WHERE public_key = ?", (K4,))
    db.apply_import(hid, discover(debian_runner()))
    assert {p.public_key: p.name for p in db.peers(iface.id)}[K4] == "drucker"


def test_import_removes_vanished_interface(db):
    hid = db.add_host("srv-a", "vpn")
    db.apply_import(hid, discover(debian_runner()))
    state = discover(debian_runner())
    state.confs.clear()
    assert [(c.kind, c.subject) for c in db.plan_import(hid, state)] == [("entfernt", "Interface wg0")]
    db.apply_import(hid, state)
    assert db.interfaces(hid) == []


def test_interface_change_detail(db):
    hid = db.add_host("srv-a", "vpn")
    db.apply_import(hid, discover(debian_runner()))
    state = discover(debian_runner(conf=fixture_text("wg0.conf").replace("51820", "51821")))
    [change] = [c for c in db.plan_import(hid, state) if c.subject == "Interface wg0"]
    assert change.kind == "geändert" and "ListenPort: 51820 → 51821" in change.detail


def test_no_secrets_in_database(db):
    hid = db.add_host("srv-a", "vpn")
    db.apply_import(hid, discover(debian_runner()))
    dump = "\n".join(db.conn.iterdump())
    assert K3 in dump
    for secret in SECRETS:
        assert secret not in dump


def test_newer_schema_rejected(tmp_path):
    path = tmp_path / "new.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="neuer"):
        Database(path)


def test_sudo_roundtrip(db):
    hid = db.add_host("srv-a", "admin@vpn.example.org", sudo=True)
    assert db.host(hid).sudo is True
    db.update_host(hid, "srv-a", "admin@vpn.example.org", None, sudo=False)
    assert db.host(hid).sudo is False


# Stage 1 schema (version 1, commit 35751cf), as stored in real databases
V1_SCHEMA = """
CREATE TABLE host (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, ssh_target TEXT NOT NULL, port INTEGER,
                   os TEXT, checked_at TEXT);
CREATE TABLE interface (id INTEGER PRIMARY KEY, host_id INTEGER NOT NULL REFERENCES host(id) ON DELETE CASCADE,
                        name TEXT NOT NULL, addresses TEXT NOT NULL DEFAULT '', listen_port INTEGER, public_key TEXT,
                        extra TEXT NOT NULL DEFAULT '[]', UNIQUE (host_id, name));
CREATE TABLE peer (id INTEGER PRIMARY KEY, interface_id INTEGER NOT NULL REFERENCES interface(id) ON DELETE CASCADE,
                   name TEXT, public_key TEXT NOT NULL, allowed_ips TEXT NOT NULL DEFAULT '', endpoint TEXT,
                   keepalive INTEGER, has_psk INTEGER NOT NULL DEFAULT 0, extra TEXT NOT NULL DEFAULT '[]',
                   UNIQUE (interface_id, public_key));
"""


def test_migration_from_version_1(tmp_path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.executescript("""
        INSERT INTO host (name, ssh_target) VALUES ('alt', 'root@vpn.example.org');
        INSERT INTO interface (host_id, name, addresses) VALUES (1, 'wg0', '10.98.0.1/24');
        PRAGMA user_version = 1;
    """)
    conn.close()
    d = Database(path)
    [host] = d.hosts()
    assert (host.name, host.sudo) == ("alt", False)
    [iface] = d.interfaces(host.id)
    assert (iface.client_endpoint, iface.client_dns) == (None, "")
    assert d.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
    d.close()


def test_client_defaults_and_add_peer(tmp_path):
    d = Database(tmp_path / "x.db")
    hid = d.add_host("srv-a", "vpn")
    d.conn.execute("INSERT INTO interface (host_id, name, addresses) VALUES (?, 'wg0', '10.98.0.1/24')", (hid,))
    [iface] = d.interfaces(hid)
    d.set_client_defaults(iface.id, "vpn.example.org:51820", "10.98.0.1")
    d.add_peer(iface.id, "handy", K1, ["10.98.0.6/32"], has_psk=True)
    [iface] = d.interfaces(hid)
    assert (iface.client_endpoint, iface.client_dns) == ("vpn.example.org:51820", "10.98.0.1")
    [peer] = d.peers(iface.id)
    assert (peer.name, peer.public_key, peer.allowed_ips, peer.has_psk) == ("handy", K1, ["10.98.0.6/32"], True)
    d.close()


def test_upsert_delete_and_disabled(tmp_path):
    from wggui.parse import PeerConf

    d = Database(tmp_path / "x.db")
    hid = d.add_host("srv-a", "vpn")
    d.conn.execute("INSERT INTO interface (host_id, name, addresses) VALUES (?, 'wg0', '10.98.0.1/24')", (hid,))
    [iface] = d.interfaces(hid)
    d.add_peer(iface.id, "handy", K1, ["10.98.0.6/32"], has_psk=True)
    d.upsert_peer(iface.id, PeerConf(K1, None, ["10.98.0.7/32"], has_psk=True, disabled=True))
    [peer] = d.peers(iface.id)
    assert (peer.name, peer.allowed_ips, peer.disabled) == ("handy", ["10.98.0.7/32"], True)  # name remains
    d.delete_peer(iface.id, K1)
    assert d.peers(iface.id) == []
    d.close()
