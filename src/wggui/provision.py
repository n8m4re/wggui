"""Stage 4: install wireguard-tools, create a new interface, server-to-server tunnel.

Same safety chain as stages 2/3: preview in the app, rollback timer on the host, confirmation by a second
SSH call. Private keys are generated on the host (`wg genkey`) and never leave it – the app only receives the
public key. A tunnel's preshared key is generated locally and sent to both hosts via stdin only.
Sources: wg(8) (genkey, pubkey), wg-quick(8) (up, down, CONFIG_FILE), systemd.unit / wg-quick@.service,
package names from wireguard.com/install and the distributions' repositories (verified in the test containers).
POSIX sh only (dash, BusyBox ash) – see CONTRIBUTING.md (safety rules for server code).
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field

from wggui.apply import TAG_RE, WritePlan, make_tag
from wggui.client import validate_endpoint, validate_name
from wggui.discover import WG_DIR
from wggui.i18n import N_, _
from wggui.parse import IFNAME_RE, is_valid_key
from wggui.remote import CommandError, Runner

log = logging.getLogger(__name__)

# Non-interactive install commands per distribution family (argv, run as root or via sudo -n).
INSTALL_ARGV: dict[str, list[str]] = {
    "debian": ["sh", "-c", "DEBIAN_FRONTEND=noninteractive apt-get update -q && "
                           "DEBIAN_FRONTEND=noninteractive apt-get install -y -q wireguard-tools"],
    "fedora": ["dnf", "-y", "install", "wireguard-tools"],
    "rhel": ["dnf", "-y", "install", "wireguard-tools"],
    "arch": ["pacman", "-S", "--noconfirm", "--needed", "wireguard-tools"],
    "alpine": ["apk", "add", "--no-cache", "wireguard-tools"],
    "suse": ["zypper", "--non-interactive", "install", "wireguard-tools"],
}


def install_command(family: str | None) -> list[str] | None:
    """argv to install wireguard-tools, or None if the family has no automatic install (manual hint instead)."""
    return list(INSTALL_ARGV[family]) if family in INSTALL_ARGV else None


def install_tools(runner: Runner, family: str | None) -> None:
    argv = install_command(family)
    if argv is None:
        raise ValueError(_("Für diese Distribution gibt es keine automatische Installation."))
    runner.run(argv)
    if not runner.run(["sh", "-c", "command -v wg >/dev/null 2>&1"], check=False).ok:
        raise ValueError(_("wireguard-tools sind nach der Installation nicht verfügbar."))
    log.info("wireguard-tools installiert (%s)", family)


# --- New interface ------------------------------------------------------------------------------------

# $1 iface, $2 config dir, $3 address (CIDR), $4 listen port, $5 tag, $6 rollback seconds, $7 rollback script,
# $8 start now (1/0), $9 enable at boot (1/0). Prints "pubkey <key>" and "started"/"enabled" on success.
CREATE_IFACE = r"""
set -eu
umask 077
i=$1; d=$2; f="$2/$1.conf"; mark="$2/.wggui-pending-$1-$5"
if [ -e "$f" ]; then echo "interface exists" >&2; exit 3; fi
command -v setsid >/dev/null 2>&1 || { echo "setsid missing" >&2; exit 6; }
command -v wg >/dev/null 2>&1 || { echo "wg missing" >&2; exit 8; }
mkdir -p "$d"
chmod 700 "$d"
key=$(wg genkey)
{
    printf '[Interface]\n'
    printf 'Address = %s\n' "$3"
    printf 'ListenPort = %s\n' "$4"
    printf 'PrivateKey = %s\n' "$key"
} > "$f.wggui-new"
mv "$f.wggui-new" "$f"
echo "$f" > "$mark"
setsid sh -c "$7" sh "$6" "$mark" "$f" "$i" </dev/null >/dev/null 2>&1 &
undo() {
    rm -f "$mark"
    wg-quick down "$f" >/dev/null 2>&1 || true
    rm -f "$f"
}
if [ "$9" = 1 ] && command -v systemctl >/dev/null 2>&1; then
    if ! systemctl enable --now "wg-quick@$i" >/dev/null 2>&1; then undo; echo "start failed" >&2; exit 4; fi
    echo "enabled"
elif [ "$8" = 1 ]; then
    if ! wg-quick up "$f" >/dev/null 2>&1; then undo; echo "start failed" >&2; exit 4; fi
    echo "started"
fi
printf 'pubkey %s\n' "$(printf '%s\n' "$key" | wg pubkey)"
"""

# Runs detached: without confirmation stop the new interface and remove its file.
# $1 seconds, $2 marker, $3 config file, $4 iface
ROLLBACK_CREATE = r"""
sleep "$1"
rm "$2" 2>/dev/null || exit 0
if command -v systemctl >/dev/null 2>&1; then systemctl disable --now "wg-quick@$4" >/dev/null 2>&1 || true; fi
wg-quick down "$3" >/dev/null 2>&1 || true
rm -f "$3"
"""

# Return codes of CREATE_IFACE → user message (translated when shown)
CREATE_MESSAGES = {
    3: N_("Interface existiert schon – anderen Namen wählen."),
    4: N_("Interface ließ sich nicht starten – Datei wieder entfernt."),
    6: N_("Auf dem Host fehlt setsid – ohne Rollback-Timer wird nichts geschrieben."),
    8: N_("wireguard-tools fehlen auf dem Host – zuerst installieren."),
}


@dataclass(frozen=True)
class NewInterface:
    host_name: str
    name: str
    address: str  # e.g. 10.98.1.1/24
    listen_port: int
    start: bool = True
    enable: bool = True
    conf_dir: str = WG_DIR
    tag: str = field(default_factory=make_tag)
    rollback_seconds: int = 120

    # Same attributes as apply.WritePlan, so PlanDialog can show the preview
    warnings: tuple[str, ...] = ()

    @property
    def conf_path(self) -> str:
        return f"{self.conf_dir}/{self.name}.conf"

    @property
    def action(self) -> str:
        return _("Neues Interface {name} anlegen").format(name=self.name)

    @property
    def running(self) -> bool:
        return False

    @property
    def preview(self) -> str:
        return "".join(f"+{line}\n" for line in (
            "[Interface]", f"Address = {self.address}", f"ListenPort = {self.listen_port}",
            "PrivateKey = " + _("(wird auf dem Server erzeugt, verlässt ihn nie)")))

    def steps(self) -> list[str]:
        steps = [_("Schlüssel auf dem Server erzeugen (wg genkey)"),
                 _("Datei {path} anlegen (Rechte 0600)").format(path=f"{self.conf_dir}/{self.name}.conf"),
                 _("Rollback-Timer: ohne Bestätigung nach {seconds} s Interface stoppen und Datei entfernen")
                 .format(seconds=self.rollback_seconds)]
        if self.enable:
            steps.append(_("Beim Booten starten und jetzt starten (systemctl enable --now wg-quick@{name}; ohne "
                           "systemd: wg-quick up)").format(name=self.name))
        elif self.start:
            steps.append(_("Jetzt starten (wg-quick up)"))
        steps.append(_("Bestätigen (neuer SSH-Aufruf) – erst dann bleibt das Interface"))
        return steps


def validate_new_interface(name: str, address: str, port: int, existing: dict[str, tuple[list[str], int | None]]
                           ) -> None:
    """existing: interface name → (addresses, listen port) on the same host (file or runtime)."""
    if not IFNAME_RE.match(name):
        raise ValueError(_("Name: Buchstaben, Ziffern, _ = + . - (max. 15 Zeichen)."))
    if name in existing:
        raise ValueError(_("Interface {name} gibt es auf diesem Host schon.").format(name=name))
    try:
        iface = ipaddress.ip_interface(address)
    except ValueError:
        raise ValueError(_("Adresse: z. B. 10.98.1.1/24")) from None
    if iface.network.prefixlen == iface.ip.max_prefixlen:
        raise ValueError(_("Adresse: mit Netzmaske angeben, z. B. 10.98.1.1/24"))
    if not 1 <= port <= 65535:
        raise ValueError(_("ListenPort: 1–65535"))
    for other, (addresses, other_port) in existing.items():
        if other_port == port:
            raise ValueError(_("ListenPort {port} nutzt schon {other}.").format(port=port, other=other))
        for a in addresses:
            try:
                net = ipaddress.ip_interface(a).network
            except ValueError:
                continue
            if net.version == iface.network.version and net.overlaps(iface.network):
                raise ValueError(_("Netz überschneidet sich mit {other} ({net}).").format(other=other, net=net))


def suggest_new_interface(existing: dict[str, tuple[list[str], int | None]]) -> tuple[str, str, int]:
    """First free wgN, a free 10.98.N.1/24 network and the first free port from 51820."""
    names = set(existing)
    n = next(i for i in range(100) if f"wg{i}" not in names)
    used_nets = []
    for addresses, _port in existing.values():
        for a in addresses:
            try:
                used_nets.append(ipaddress.ip_interface(a).network)
            except ValueError:
                continue
    net = next(f"10.98.{i}.1/24" for i in range(1, 255)
               if not any(ipaddress.ip_network(f"10.98.{i}.0/24").overlaps(u) for u in used_nets if u.version == 4))
    ports = {p for _a, p in existing.values() if p}
    port = next(p for p in range(51820, 52820) if p not in ports)
    return f"wg{n}", net, port


def create_interface(runner: Runner, new: NewInterface) -> str:
    """Creates and (optionally) starts the interface. Returns the interface public key."""
    if not IFNAME_RE.match(new.name) or not TAG_RE.match(new.tag):
        raise ValueError("invalid interface name or tag")
    ipaddress.ip_interface(new.address)
    try:
        out = runner.run(["sh", "-c", CREATE_IFACE, "sh", new.name, new.conf_dir, new.address,
                          str(int(new.listen_port)), new.tag, str(int(new.rollback_seconds)), ROLLBACK_CREATE,
                          "1" if new.start else "0", "1" if new.enable else "0"]).stdout
    except CommandError as err:
        text = CREATE_MESSAGES.get(err.result.returncode)
        if text:
            raise ValueError(_(text)) from err
        raise
    key = next((line.split()[1] for line in out.splitlines() if line.startswith("pubkey ")), "")
    if not is_valid_key(key):
        raise ValueError(_("Public Key des neuen Interfaces nicht lesbar."))
    log.info("%s: Interface %s angelegt (%s)", new.host_name, new.name, new.address)
    return key


def confirm_interface(runner: Runner, new: NewInterface) -> None:
    from wggui.apply import CONFIRM

    runner.run(["sh", "-c", CONFIRM, "sh", new.name, new.conf_dir, new.tag])


# --- Server-to-server tunnel ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TunnelSide:
    host_name: str
    iface: str
    public_key: str
    endpoint: str  # how the other side reaches this one (host:port)
    allowed_ips: tuple[str, ...]  # what is routed to this side (its tunnel address /32 + optional networks)


def tunnel_peer_block(own_label: str, other: TunnelSide, psk: str | None, keepalive: bool = True) -> str:
    """[Peer] block for one side, describing the OTHER side. Secret if psk is set (stdin only)."""
    if not is_valid_key(other.public_key):
        raise ValueError("invalid PublicKey")
    validate_endpoint(other.endpoint)
    allowed = [str(ipaddress.ip_network(a, strict=False)) for a in other.allowed_ips]
    if not allowed:
        raise ValueError(_("AllowedIPs fehlen."))
    lines = [f"# {validate_name(own_label)}", "[Peer]", f"PublicKey = {other.public_key}"]
    if psk:
        lines.append(f"PresharedKey = {psk}")
    lines += [f"Endpoint = {other.endpoint}", f"AllowedIPs = {', '.join(allowed)}"]
    if keepalive:
        lines.append("PersistentKeepalive = 25")
    return "\n".join(lines) + "\n"


def tunnel_plans(a: TunnelSide, b: TunnelSide, psk: str | None, hashes: tuple[str, str], running: tuple[bool, bool],
                 keepalive: bool = True) -> tuple[WritePlan, WritePlan]:
    """Two append plans: on A a peer for B, on B a peer for A (same PSK on both sides)."""
    plans = []
    for (own, other), conf_hash, run in zip(((a, b), (b, a)), hashes, running, strict=True):
        label = f"tunnel-{other.host_name}-{other.iface}"[:40]
        block = tunnel_peer_block(label, other, psk, keepalive)
        hidden = _("(neu, wird nicht angezeigt)")
        preview = block.replace(f"PresharedKey = {psk}", f"PresharedKey = {hidden}") if psk else block
        plans.append(WritePlan(own.host_name, own.iface, WG_DIR, "".join(f"+{ln}\n" for ln in preview.splitlines()),
                               block, conf_hash, run, make_tag(),
                               action=_("Tunnel zu {host}/{iface} anlegen").format(host=other.host_name,
                                                                                    iface=other.iface)))
    return plans[0], plans[1]


__all__ = ["CREATE_IFACE", "INSTALL_ARGV", "NewInterface", "ROLLBACK_CREATE", "TunnelSide",
           "confirm_interface", "create_interface", "install_command", "install_tools", "suggest_new_interface",
           "tunnel_peer_block", "tunnel_plans", "validate_new_interface"]
