"""Sync app ↔ server (stage 3): find per-peer differences and choose a direction for each row.

Three states: app (SQLite), server file (`/etc/wireguard/<if>.conf`), runtime (`wg show`).
No Qt or SSH dependency, so this is testable. Server-bound changes become `edit.PeerOp`s (one plan).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from wggui.db import Peer
from wggui.edit import PeerOp
from wggui.i18n import N_, _
from wggui.parse import InterfaceConf, PeerConf, RuntimeInterface

# Choice IDs (German translation source; display via _(), logic compares the IDs)
TO_APP = N_("In die App übernehmen")
TO_SERVER = N_("Server anpassen")
REMOVE_APP = N_("Aus der App entfernen")
ADD_SERVER = N_("Auf dem Server anlegen (ohne PSK)")
REMOVE_SERVER = N_("Vom Server entfernen")
SYNC_RUNTIME = N_("Laufzeit an die Datei angleichen")
IGNORE = N_("Nichts tun")


def _label(name: str | None, key: str) -> str:
    return name or _("Peer {v1}…").format(v1=key[:8])


def _ips(values: list[str]) -> str:
    return ", ".join(values) or "–"


def _nets(values: list[str]) -> set[str]:
    out = set()
    for v in values:
        try:
            out.add(str(ipaddress.ip_network(v, strict=False)))
        except ValueError:
            out.add(v)
    return out


@dataclass(frozen=True)
class Drift:
    key: str | None  # None = runtime row
    label: str
    kind: str  # "nur_server" | "nur_app" | "unterschied" | "laufzeit" (state IDs)
    detail: str
    choices: tuple[str, ...]
    default: str


def _differences(app: Peer, srv: PeerConf) -> tuple[list[str], bool]:
    """Describe whether app values can be written to the server."""
    diffs, writable = [], True
    if srv.name and srv.name != app.name:
        diffs.append(_("Name: App {v1} / Server {srv_name}").format(v1=app.name or '–', srv_name=srv.name))
    if srv.allowed_ips != app.allowed_ips:
        diffs.append(_("AllowedIPs: App {v1} / Server {v2}").format(v1=_ips(app.allowed_ips), v2=_ips(srv.allowed_ips)))
    if srv.keepalive != app.keepalive:
        diffs.append(_("Keepalive: App {v1} / Server {v2}").format(v1=app.keepalive or '–', v2=srv.keepalive or '–'))
    if srv.disabled != app.disabled:
        diffs.append(_("Gesperrt: App {v1} / Server {v2}")
            .format(v1='ja' if app.disabled else 'nein', v2='ja' if srv.disabled else 'nein'))
    only_app_side = []
    if srv.endpoint != app.endpoint:
        only_app_side.append(_("Endpoint: App {v1} / Server {v2}")
            .format(v1=app.endpoint or '–', v2=srv.endpoint or '–'))
    if srv.has_psk != app.has_psk:
        only_app_side.append("PresharedKey: " + (_("nur auf dem Server") if srv.has_psk else _("nur in der App")))
    if srv.extra != app.extra:
        only_app_side.append(_("weitere Optionen"))
    if only_app_side and not diffs:
        writable = False
    return diffs + only_app_side, writable


def compute(app_peers: list[Peer], conf: InterfaceConf, runtime: RuntimeInterface | None) -> list[Drift]:
    drifts: list[Drift] = []
    app = {p.public_key: p for p in app_peers}
    srv = {p.public_key: p for p in conf.peers}
    for key, p in srv.items():
        a = app.get(key)
        if a is None:
            drifts.append(Drift(key, _label(p.name, key), "nur_server", _("nur auf dem Server: {v1}")
                .format(v1=_ips(p.allowed_ips)),
                                (TO_APP, REMOVE_SERVER, IGNORE), TO_APP))
            continue
        diffs, writable = _differences(a, p)
        if diffs:
            choices = (TO_APP, TO_SERVER, IGNORE) if writable else (TO_APP, IGNORE)
            drifts.append(Drift(key, _label(a.name or p.name, key), "unterschied", "; ".join(diffs), choices, TO_APP))
    for key, a in app.items():
        if key not in srv:
            drifts.append(Drift(key, _label(a.name, key), "nur_app",
                                _("nur in der App: {v1} – auf dem Server nicht (mehr) vorhanden")
                                    .format(v1=_ips(a.allowed_ips)),
                                (REMOVE_APP, ADD_SERVER, IGNORE), REMOVE_APP))
    if runtime is not None:
        active = {k for k, p in srv.items() if not p.disabled}
        extra_rt = sorted(set(runtime.peers) - active)
        missing = sorted(active - set(runtime.peers))
        differing = []  # For example, changed via `wg set` without changing the file
        for k in sorted(active & set(runtime.peers)):
            rt, f = runtime.peers[k], srv[k]
            what = [name for name, bad in (("AllowedIPs", _nets(rt.allowed_ips) != _nets(f.allowed_ips)),
                                           (_("Keepalive"), (rt.keepalive or None) != (f.keepalive or None))) if bad]
            if what:
                differing.append(f"{_label(f.name, k)} ({', '.join(what)})")
        if extra_rt or missing or differing:
            parts = []
            if extra_rt:
                parts.append(_("nur in der Laufzeit (fehlen in der Datei): ") +
                             ", ".join(_label(app[k].name if k in app else None, k) for k in extra_rt))
            if missing:
                parts.append(_("in der Datei, aber nicht geladen: ") +
                             ", ".join(_label(srv[k].name, k) for k in missing))
            if differing:
                parts.append(_("Laufzeit weicht von der Datei ab: ") + ", ".join(differing))
            drifts.append(Drift(None, _("Laufzeit (wg show)"), "laufzeit", "; ".join(parts), (IGNORE, SYNC_RUNTIME),
                                IGNORE))
    return drifts


@dataclass
class Resolution:
    """Apply choices: DB changes are local; `ops`/`sync_runtime` go through the plan."""

    db_upserts: list[PeerConf] = field(default_factory=list)
    db_deletes: list[str] = field(default_factory=list)
    ops: list[PeerOp] = field(default_factory=list)
    sync_runtime: bool = False

    @property
    def writes_server(self) -> bool:
        return bool(self.ops) or self.sync_runtime


def resolve(drifts: list[Drift], choice: dict[int, str], app_peers: list[Peer], conf: InterfaceConf) -> Resolution:
    app = {p.public_key: p for p in app_peers}
    srv = {p.public_key: p for p in conf.peers}
    res = Resolution()
    for i, d in enumerate(drifts):
        c = choice.get(i, d.default)
        if c not in d.choices:
            raise ValueError(_("Auswahl {c!r} passt nicht zu {d_label}").format(c=c, d_label=d.label))
        if c == IGNORE:
            continue
        if c == SYNC_RUNTIME:
            res.sync_runtime = True
        elif c == TO_APP:
            res.db_upserts.append(srv[d.key])
        elif c == REMOVE_APP:
            res.db_deletes.append(d.key)
        elif c == REMOVE_SERVER:
            res.ops.append(PeerOp("remove", d.key))
        elif c == ADD_SERVER:
            a = app[d.key]
            res.ops.append(PeerOp("add", d.key, a.name or "", tuple(a.allowed_ips), a.keepalive))
        elif c == TO_SERVER:
            a, s = app[d.key], srv[d.key]
            state = "" if a.disabled == s.disabled else ("off" if a.disabled else "on")
            res.ops.append(PeerOp("edit", d.key, a.name or "", tuple(a.allowed_ips), a.keepalive, state))
    return res
