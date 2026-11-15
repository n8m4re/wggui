"""SQLite storage for hosts, interfaces, and peers. Never stores private keys or preshared keys.

Location: $WGGUI_DB, otherwise $XDG_DATA_HOME/wireguard-gui/wggui.db (default ~/.local/share/…).
Access only from the GUI thread; worker threads run SSH only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wggui.discover import HostState
from wggui.i18n import N_, _
from wggui.parse import InterfaceConf, PeerConf
from wggui.remote import TARGET_RE

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
SCHEMA_VERSION = 5

# Sequential migrations: index = previous version
# 1 → 2: sudo per host; 2 → 3: new-client defaults per interface (external endpoint, DNS);
# 3 → 4: disabled peer (commented with #wggui-off); 4 → 5: per-host SSH key file (path only)
_MIGRATIONS = {
    1: "ALTER TABLE host ADD COLUMN sudo INTEGER NOT NULL DEFAULT 0;",
    2: "ALTER TABLE interface ADD COLUMN client_endpoint TEXT; "
       "ALTER TABLE interface ADD COLUMN client_dns TEXT NOT NULL DEFAULT '';",
    3: "ALTER TABLE peer ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0;",
    4: "ALTER TABLE host ADD COLUMN identity_file TEXT;",
}

_SCHEMA = """
CREATE TABLE host (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    ssh_target TEXT NOT NULL,
    port INTEGER,
    os TEXT,
    checked_at TEXT,
    sudo INTEGER NOT NULL DEFAULT 0,
    identity_file TEXT
);
CREATE TABLE interface (
    id INTEGER PRIMARY KEY,
    host_id INTEGER NOT NULL REFERENCES host(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    addresses TEXT NOT NULL DEFAULT '',
    listen_port INTEGER,
    public_key TEXT,
    extra TEXT NOT NULL DEFAULT '[]',
    client_endpoint TEXT,
    client_dns TEXT NOT NULL DEFAULT '',
    UNIQUE (host_id, name)
);
CREATE TABLE peer (
    id INTEGER PRIMARY KEY,
    interface_id INTEGER NOT NULL REFERENCES interface(id) ON DELETE CASCADE,
    name TEXT,
    public_key TEXT NOT NULL,
    allowed_ips TEXT NOT NULL DEFAULT '',
    endpoint TEXT,
    keepalive INTEGER,
    has_psk INTEGER NOT NULL DEFAULT 0,
    extra TEXT NOT NULL DEFAULT '[]',
    disabled INTEGER NOT NULL DEFAULT 0,
    UNIQUE (interface_id, public_key)
);
"""


def default_db_path() -> Path:
    if env := os.environ.get("WGGUI_DB"):
        return Path(env)
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "wireguard-gui" / "wggui.db"


@dataclass(frozen=True)
class Host:
    id: int
    name: str
    ssh_target: str
    port: int | None
    os: str | None
    checked_at: str | None
    sudo: bool = False
    identity_file: str | None = None  # None = system (SSH agent, ssh config); otherwise path, never contents


@dataclass(frozen=True)
class Interface:
    id: int
    host_id: int
    name: str
    addresses: list[str]
    listen_port: int | None
    public_key: str | None
    extra: list[tuple[str, str]]
    client_endpoint: str | None = None  # Default for new clients (externally reachable host:port)
    client_dns: str = ""


@dataclass(frozen=True)
class Peer:
    id: int
    interface_id: int
    name: str | None
    public_key: str
    allowed_ips: list[str]
    endpoint: str | None
    keepalive: int | None
    has_psk: bool
    extra: list[tuple[str, str]]
    disabled: bool = False


@dataclass(frozen=True)
class Change:
    kind: str  # kind id (German N_ source string) for new / changed / removed
    subject: str
    detail: str = ""


_UPSERT_PEER = (
    "INSERT INTO peer (interface_id, name, public_key, allowed_ips, endpoint, keepalive, has_psk, extra, disabled) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (interface_id, public_key) DO UPDATE SET name = excluded.name, "
    "allowed_ips = excluded.allowed_ips, endpoint = excluded.endpoint, keepalive = excluded.keepalive, "
    "has_psk = excluded.has_psk, extra = excluded.extra, disabled = excluded.disabled"
)


def _join(values: list[str]) -> str:
    return ", ".join(values)


def _split(text: str) -> list[str]:
    return [v.strip() for v in text.split(",") if v.strip()]


def _extra(text: str) -> list[tuple[str, str]]:
    return [(k, v) for k, v in json.loads(text)]


def _peer_label(name: str | None, public_key: str) -> str:
    return name or public_key[:8] + "…"


def validate_host(name: str, ssh_target: str, port: int | None) -> None:
    if not NAME_RE.match(name):
        raise ValueError(_("Name: Buchstaben, Ziffern, . _ - (max. 63 Zeichen)"))
    if not TARGET_RE.match(ssh_target):
        raise ValueError(_("SSH-Ziel: z. B. root@server.example.org oder ein Alias aus ~/.ssh/config"))
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(_("Port: 1–65535"))


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = None if str(path) == ":memory:" else Path(path)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with self.conn:
                self.conn.executescript(_SCHEMA)
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if version > SCHEMA_VERSION:
            raise RuntimeError(_("Datenbank ist neuer (Version {version}) als die App ({SCHEMA_VERSION})")
                .format(version=version, SCHEMA_VERSION=SCHEMA_VERSION))
        while version < SCHEMA_VERSION:
            with self.conn:
                self.conn.executescript(_MIGRATIONS[version])
                version += 1
                self.conn.execute(f"PRAGMA user_version = {version}")
            log.info("Datenbank auf Schema-Version %d migriert", version)

    def close(self) -> None:
        self.conn.close()

    # --- Hosts ---------------------------------------------------------------------------------

    def hosts(self) -> list[Host]:
        rows = self.conn.execute("SELECT * FROM host ORDER BY name").fetchall()
        return [Host(r["id"], r["name"], r["ssh_target"], r["port"], r["os"], r["checked_at"], bool(r["sudo"]),
                     r["identity_file"])
                for r in rows]

    def host(self, host_id: int) -> Host:
        for h in self.hosts():
            if h.id == host_id:
                return h
        raise KeyError(host_id)

    def add_host(self, name: str, ssh_target: str, port: int | None = None, sudo: bool = False,
                 identity_file: str | None = None) -> int:
        validate_host(name, ssh_target, port)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO host (name, ssh_target, port, sudo, identity_file) VALUES (?, ?, ?, ?, ?)",
                (name, ssh_target, port, int(sudo), identity_file or None))
        return int(cur.lastrowid or 0)

    def update_host(self, host_id: int, name: str, ssh_target: str, port: int | None, sudo: bool = False,
                    identity_file: str | None = None) -> None:
        validate_host(name, ssh_target, port)
        with self.conn:
            self.conn.execute(
                "UPDATE host SET name = ?, ssh_target = ?, port = ?, sudo = ?, identity_file = ? WHERE id = ?",
                (name, ssh_target, port, int(sudo), identity_file or None, host_id))

    def delete_host(self, host_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM host WHERE id = ?", (host_id,))

    def set_host_os(self, host_id: int, os_name: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE host SET os = ?, checked_at = ? WHERE id = ?",
                              (os_name, datetime.now(UTC).isoformat(timespec="seconds"), host_id))

    # --- Interfaces and peers ------------------------------------------------------------------

    def interfaces(self, host_id: int) -> list[Interface]:
        rows = self.conn.execute("SELECT * FROM interface WHERE host_id = ? ORDER BY name", (host_id,)).fetchall()
        return [Interface(r["id"], r["host_id"], r["name"], _split(r["addresses"]), r["listen_port"],
                          r["public_key"], _extra(r["extra"]), r["client_endpoint"], r["client_dns"]) for r in rows]

    def set_client_defaults(self, interface_id: int, endpoint: str, dns: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE interface SET client_endpoint = ?, client_dns = ? WHERE id = ?",
                              (endpoint, dns, interface_id))

    def add_peer(self, interface_id: int, name: str, public_key: str, allowed_ips: list[str],
                 has_psk: bool) -> int:
        """Store a new client after a successful server write (public key only; never private key/PSK)."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO peer (interface_id, name, public_key, allowed_ips, has_psk) VALUES (?, ?, ?, ?, ?)",
                (interface_id, name, public_key, _join(allowed_ips), int(has_psk)))
        log.info("Peer %s in der App gespeichert", name)
        return int(cur.lastrowid or 0)

    def peers(self, interface_id: int) -> list[Peer]:
        rows = self.conn.execute("SELECT * FROM peer WHERE interface_id = ? ORDER BY name, public_key",
                                 (interface_id,)).fetchall()
        return [Peer(r["id"], r["interface_id"], r["name"], r["public_key"], _split(r["allowed_ips"]),
                     r["endpoint"], r["keepalive"], bool(r["has_psk"]), _extra(r["extra"]), bool(r["disabled"]))
                for r in rows]

    # --- Import server → app -------------------------------------------------------------------

    def plan_import(self, host_id: int, state: HostState) -> list[Change]:
        """Preview what importing from the server would change in the app; make no changes."""
        changes: list[Change] = []
        existing = {i.name: i for i in self.interfaces(host_id)}
        for name, conf in sorted(state.confs.items()):
            pubkey = state.public_keys.get(name)
            old = existing.get(name)
            if old is None:
                changes.append(Change(N_("neu"), _("Interface {name}").format(name=name),
                                      _("{v1}, {n_peers} Peers")
                                          .format(v1=_join(conf.addresses) or _("ohne Adresse"),
                                                  n_peers=len(conf.peers))))
                changes += [Change(N_("neu"), _("Peer {v1} ({name})")
                    .format(v1=_peer_label(p.name, p.public_key), name=name), _join(p.allowed_ips))
                            for p in conf.peers]
                continue
            diffs = []
            if old.addresses != conf.addresses:
                diffs.append(_("Adresse: {v1} → {v2}").format(v1=_join(old.addresses), v2=_join(conf.addresses)))
            if old.listen_port != conf.listen_port:
                diffs.append(_("ListenPort: {old_listen_port} → {conf_listen_port}")
                    .format(old_listen_port=old.listen_port, conf_listen_port=conf.listen_port))
            if pubkey and old.public_key != pubkey:
                diffs.append(_("PublicKey geändert"))
            if old.extra != conf.extra:
                diffs.append(_("weitere Optionen (DNS, MTU, PostUp …)"))
            if diffs:
                changes.append(Change(N_("geändert"), _("Interface {name}").format(name=name), "; ".join(diffs)))
            changes += self._peer_changes(old, conf)
        for name in sorted(set(existing) - set(state.confs)):
            changes.append(Change(N_("entfernt"), _("Interface {name}")
                .format(name=name), _("auf dem Server nicht mehr vorhanden")))
        return changes

    def _peer_changes(self, iface: Interface, conf: InterfaceConf) -> list[Change]:
        changes: list[Change] = []
        old_peers = {p.public_key: p for p in self.peers(iface.id)}
        for p in conf.peers:
            old = old_peers.get(p.public_key)
            shown = _peer_label(p.name or (old.name if old else None), p.public_key)
            label = _("Peer {name} ({iface})").format(name=shown, iface=iface.name)
            if old is None:
                changes.append(Change("neu", label, _join(p.allowed_ips)))
                continue
            diffs = []
            if p.name and p.name != old.name:
                diffs.append(_("Name: {v1} → {p_name}").format(v1=old.name or '–', p_name=p.name))
            if p.allowed_ips != old.allowed_ips:
                diffs.append(_("AllowedIPs: {v1} → {v2}").format(v1=_join(old.allowed_ips), v2=_join(p.allowed_ips)))
            if p.endpoint != old.endpoint:
                diffs.append(_("Endpoint: {v1} → {v2}").format(v1=old.endpoint or '–', v2=p.endpoint or '–'))
            if p.keepalive != old.keepalive:
                diffs.append(_("Keepalive: {v1} → {v2}").format(v1=old.keepalive or '–', v2=p.keepalive or '–'))
            if p.has_psk != old.has_psk:
                diffs.append("PresharedKey " + ("hinzugekommen" if p.has_psk else "entfernt"))
            if p.extra != old.extra:
                diffs.append(_("weitere Optionen"))
            if p.disabled != old.disabled:
                diffs.append("gesperrt" if p.disabled else "entsperrt")
            if diffs:
                changes.append(Change(N_("geändert"), label, "; ".join(diffs)))
        new_keys = {p.public_key for p in conf.peers}
        for key, old in old_peers.items():
            if key not in new_keys:
                changes.append(Change(N_("entfernt"), _("Peer {v1} ({iface_name})")
                    .format(v1=_peer_label(old.name, key), iface_name=iface.name),
                                      _("auf dem Server nicht mehr vorhanden")))
        return changes

    def apply_import(self, host_id: int, state: HostState) -> None:
        """Import server configs into the app; keep app names when configs have none."""
        with self.conn:
            existing = {i.name: i for i in self.interfaces(host_id)}
            for name in set(existing) - set(state.confs):
                self.conn.execute("DELETE FROM interface WHERE id = ?", (existing[name].id,))
            for name, conf in state.confs.items():
                pubkey = state.public_keys.get(name) or (
                    state.runtime[name].public_key if name in state.runtime else None)
                values = (_join(conf.addresses), conf.listen_port, pubkey, json.dumps(conf.extra))
                if name in existing:
                    iface_id = existing[name].id
                    self.conn.execute(
                        "UPDATE interface SET addresses = ?, listen_port = ?, public_key = ?, extra = ? WHERE id = ?",
                        (*values, iface_id))
                else:
                    cur = self.conn.execute(
                        "INSERT INTO interface (host_id, name, addresses, listen_port, public_key, extra) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (host_id, name, *values))
                    iface_id = int(cur.lastrowid or 0)
                self._import_peers(iface_id, conf.peers)

    def _import_peers(self, iface_id: int, peers: list[PeerConf]) -> None:
        old_names = {r["public_key"]: r["name"] for r in
                     self.conn.execute("SELECT public_key, name FROM peer WHERE interface_id = ?", (iface_id,))}
        keys = [p.public_key for p in peers]
        self.conn.execute(
            f"DELETE FROM peer WHERE interface_id = ? AND public_key NOT IN ({', '.join('?' * len(keys))})",  # noqa: S608
            (iface_id, *keys))
        for p in peers:
            name = p.name or old_names.get(p.public_key)
            self.conn.execute(
                _UPSERT_PEER,
                (iface_id, name, p.public_key, _join(p.allowed_ips), p.endpoint, p.keepalive, int(p.has_psk),
                 json.dumps(p.extra), int(p.disabled)))

    def upsert_peer(self, interface_id: int, peer: PeerConf) -> None:
        """Import a peer from the server after a write or sync. Keep the app name if the config
        has none."""
        old = self.conn.execute("SELECT name FROM peer WHERE interface_id = ? AND public_key = ?",
                                (interface_id, peer.public_key)).fetchone()
        name = peer.name or (old["name"] if old else None)
        with self.conn:
            self.conn.execute(_UPSERT_PEER, (interface_id, name, peer.public_key, _join(peer.allowed_ips),
                                             peer.endpoint, peer.keepalive, int(peer.has_psk), json.dumps(peer.extra),
                                             int(peer.disabled)))

    def delete_peer(self, interface_id: int, public_key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM peer WHERE interface_id = ? AND public_key = ?", (interface_id, public_key))
