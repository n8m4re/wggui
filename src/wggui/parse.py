"""Parse wg-quick configs and `wg show all dump` output.

Sources: wg(8) (CONFIGURATION FILE FORMAT, `show … dump`), wg-quick(8) (Address, DNS, MTU,
Table, Pre/PostUp/Down, SaveConfig). Key names are case-insensitive; wg-quick strips text after `#`.

Security: never store PrivateKey/PresharedKey values, only whether they exist.
This is the second safeguard; the first is host-side redaction (discover.py).
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field

# Interface names allowed by wg-quick (config filename check in the script)
IFNAME_RE = re.compile(r"^[a-zA-Z0-9_=+.-]{1,15}$")

_INTERFACE_KEYS = ("Address", "ListenPort", "PrivateKey", "DNS", "MTU", "Table", "PreUp", "PostUp",
                   "PreDown", "PostDown", "SaveConfig", "FwMark")
_PEER_KEYS = ("PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive")
_CANON = {k.lower(): k for k in (*_INTERFACE_KEYS, *_PEER_KEYS)}

# Peer names from comments: “# name”, PiVPN “### begin name ###”, “# Name = x”, wg-easy “# Client: x (id)”
_PIVPN_BEGIN = re.compile(r"^#+\s*begin\s+(.+?)\s*#+$", re.I)
_PIVPN_END = re.compile(r"^#+\s*end\s+.*#+$", re.I)
_NAME_KV = re.compile(r"^#+\s*(?:name|client|friendly_?name)\s*[:=]\s*(.+)$", re.I)
_TRAILING_ID = re.compile(r"\s*\([0-9a-fA-F-]{8,}\)$")
# Commented setting such as “# AllowedIPs = 0.0.0.0/0” or “#PrivateKey = (redacted)” is not a name
_COMMENTED_SETTING = re.compile(r"^#+\s*[A-Za-z]+\s*=", re.I)
NAME_MAX = 64
# Disabled peer (stage 3, edit.py): every block line starts with this prefix
OFF_MARK = "#wggui-off"


def is_valid_key(value: str) -> bool:
    """Check for a WireGuard key: Base64 encoding of exactly 32 bytes (wg(8))."""
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


@dataclass
class PeerConf:
    public_key: str = ""
    name: str | None = None
    allowed_ips: list[str] = field(default_factory=list)
    endpoint: str | None = None
    keepalive: int | None = None
    has_psk: bool = False
    extra: list[tuple[str, str]] = field(default_factory=list)
    disabled: bool = False  # Commented out with OFF_MARK (disabled)


@dataclass
class InterfaceConf:
    addresses: list[str] = field(default_factory=list)
    listen_port: int | None = None
    has_private_key: bool = False
    save_config: bool = False
    extra: list[tuple[str, str]] = field(default_factory=list)  # DNS, MTU, PostUp, etc. in original order
    peers: list[PeerConf] = field(default_factory=list)


def _comment_name(line: str) -> str | None:
    if _PIVPN_END.match(line):
        return None
    m = _PIVPN_BEGIN.match(line) or _NAME_KV.match(line)
    if not m and _COMMENTED_SETTING.match(line):
        return None
    text = m.group(1) if m else line.lstrip("#")
    text = _TRAILING_ID.sub("", text.strip())
    return text[:NAME_MAX] or None


def _split_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _int(key: str, value: str, lineno: int) -> int:
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"Zeile {lineno}: {key} ist keine Zahl: {value!r}") from None


def parse_conf(text: str) -> InterfaceConf:
    conf = InterfaceConf()
    section: str | None = None
    peer: PeerConf | None = None
    peer_has_keys = False
    pending: list[str] = []  # Comments before the next [Peer]

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        off = line.startswith(OFF_MARK)
        if off:
            line = line[len(OFF_MARK):].strip()
        if not line:
            continue
        if line.startswith("#"):
            if section == "peer" and peer is not None and peer.name is None and not peer_has_keys:
                peer.name = _comment_name(line)
            else:
                pending.append(line)
            continue
        content = line.split("#", 1)[0].strip()
        if content.startswith("["):
            header = content.lower()
            if header == "[interface]":
                section, pending = "interface", []
            elif header == "[peer]":
                peer = PeerConf(disabled=off)
                for comment in reversed(pending):
                    if name := _comment_name(comment):
                        peer.name = name
                        break
                conf.peers.append(peer)
                section, pending, peer_has_keys = "peer", [], False
            else:
                raise ValueError(f"Zeile {lineno}: unbekannter Abschnitt {content}")
            continue
        if "=" not in content:
            raise ValueError(f"Zeile {lineno}: erwartet „Schlüssel = Wert“")
        if section is None:
            raise ValueError(f"Zeile {lineno}: Eintrag vor [Interface]")
        raw_key, value = (s.strip() for s in content.split("=", 1))
        key = _CANON.get(raw_key.lower(), raw_key)
        pending = []

        if section == "interface":
            match key:
                case "PrivateKey":
                    conf.has_private_key = True  # Discard the value
                case "Address":
                    conf.addresses += _split_list(value)
                case "ListenPort":
                    conf.listen_port = _int(key, value, lineno)
                case _:
                    if key == "SaveConfig":
                        conf.save_config = value.lower() == "true"
                    conf.extra.append((key, value))
        elif peer is not None:
            peer_has_keys = True
            match key:
                case "PublicKey":
                    if not is_valid_key(value):
                        raise ValueError(f"Zeile {lineno}: ungültiger PublicKey")
                    peer.public_key = value
                case "PresharedKey":
                    peer.has_psk = True  # Discard the value
                case "AllowedIPs":
                    peer.allowed_ips += _split_list(value)
                case "Endpoint":
                    peer.endpoint = value
                case "PersistentKeepalive":
                    peer.keepalive = None if value.lower() == "off" else _int(key, value, lineno)
                case _:
                    peer.extra.append((key, value))

    for i, p in enumerate(conf.peers, start=1):
        if not p.public_key:
            raise ValueError(f"Peer {i} ohne PublicKey")
    return conf


@dataclass
class RuntimePeer:
    public_key: str
    endpoint: str | None
    allowed_ips: list[str]
    latest_handshake: int  # Unix timestamp; 0 = never
    rx_bytes: int
    tx_bytes: int
    keepalive: int | None


@dataclass
class RuntimeInterface:
    name: str
    public_key: str | None
    listen_port: int | None
    peers: dict[str, RuntimePeer] = field(default_factory=dict)


def _none(value: str) -> str | None:
    return None if value in ("(none)", "") else value


def parse_dump(text: str) -> dict[str, RuntimeInterface]:
    """Parse `wg show all dump`: 5 fields per interface, 9 per peer, tab separated (wg(8))."""
    result: dict[str, RuntimeInterface] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        f = line.split("\t")
        if len(f) == 5:
            # interface, private-key (redacted), public-key, listen-port, fwmark
            port = int(f[3]) if f[3].isdigit() and f[3] != "0" else None
            result[f[0]] = RuntimeInterface(f[0], _none(f[2]), port)
        elif len(f) == 9:
            # interface, public-key, preshared-key (redacted), endpoint, allowed-ips, handshake, rx, tx, keepalive
            iface = result.setdefault(f[0], RuntimeInterface(f[0], None, None))
            iface.peers[f[1]] = RuntimePeer(
                public_key=f[1],
                endpoint=_none(f[3]),
                allowed_ips=[] if f[4] == "(none)" else _split_list(f[4]),
                latest_handshake=int(f[5]),
                rx_bytes=int(f[6]),
                tx_bytes=int(f[7]),
                keepalive=None if f[8] == "off" else int(f[8]),
            )
        else:
            raise ValueError(f"unerwartete dump-Zeile mit {len(f)} Feldern")
    return result
