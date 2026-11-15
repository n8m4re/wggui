"""Add a client (phone, laptop, etc.) to a server interface: free address, server peer block,
and client config. No Qt or SSH dependency, so this is testable.

Formats from wg(8) and wg-quick(8): `[Interface]` has PrivateKey/Address/DNS; `[Peer]` has PublicKey,
PresharedKey, AllowedIPs, Endpoint, PersistentKeepalive. A 25 s keepalive is “a sensible interval that works
with a wide variety of firewalls” (wg(8), PersistentKeepalive).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from wggui.i18n import _
from wggui.parse import is_valid_key

# Name becomes a comment above [Peer] → no newlines, “#”, “[”, or “=”
CLIENT_NAME_RE = re.compile(r"^[\w][\w .@+-]{0,39}$")
ENDPOINT_RE = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([0-9]{1,5})$")
KEEPALIVE = 25
FULL_TUNNEL = ["0.0.0.0/0", "::/0"]

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def interface_networks(addresses: Iterable[str]) -> list[Network]:
    """Get server interface networks from `Address = 10.98.0.1/24, fd00::1/64`."""
    nets = []
    for a in addresses:
        try:
            nets.append(ipaddress.ip_interface(a).network)
        except ValueError:
            continue
    return nets


def used_networks(interface_addresses: Iterable[str], peer_allowed_ips: Iterable[str]) -> list[Network]:
    """Collect occupied addresses: interface addresses (/32 or /128) and peer AllowedIPs."""
    used: list[Network] = []
    for a in interface_addresses:
        try:
            used.append(ipaddress.ip_network(ipaddress.ip_interface(a).ip))
        except ValueError:
            continue
    for a in peer_allowed_ips:
        try:
            used.append(ipaddress.ip_network(a, strict=False))
        except ValueError:
            continue
    return used


def is_free(ip: Address, used: Iterable[Network]) -> bool:
    return not any(ip.version == n.version and ip in n for n in used)


def next_free(network: Network, used: list[Network], limit: int = 65536) -> Address | None:
    """Find the first free host address, excluding network/broadcast; bound the /64 search."""
    for i, ip in enumerate(network.hosts()):
        if i >= limit:
            return None
        if is_free(ip, used):
            return ip
    return None


def suggest_addresses(interface_addresses: list[str], peer_allowed_ips: Iterable[str]) -> list[str]:
    """Find the first free address in each interface network, e.g. ["10.98.0.6/32"]."""
    used = used_networks(interface_addresses, peer_allowed_ips)
    out = []
    for net in interface_networks(interface_addresses):
        ip = next_free(net, used)
        if ip is not None:
            out.append(f"{ip}/{ip.max_prefixlen}")
    return out


def validate_addresses(addresses: list[str], interface_addresses: list[str],
                       peer_allowed_ips: Iterable[str]) -> list[str]:
    """Validate free client addresses (/32 or /128) in the interface networks; return normalized values."""
    if not addresses:
        raise ValueError(_("Adresse fehlt."))
    nets = interface_networks(interface_addresses)
    used = used_networks(interface_addresses, peer_allowed_ips)
    out = []
    for text in addresses:
        try:
            iface = ipaddress.ip_interface(text if "/" in text else f"{text}/{32 if ':' not in text else 128}")
        except ValueError:
            raise ValueError(_("Adresse {text}: keine gültige IP-Adresse.").format(text=text)) from None
        ip = iface.ip
        if iface.network.prefixlen != ip.max_prefixlen:
            raise ValueError(_("Adresse {text}: nur eine einzelne Adresse (/{ip_max_prefixlen}).")
                .format(text=text, ip_max_prefixlen=ip.max_prefixlen))
        if not any(ip.version == n.version and ip in n for n in nets):
            raise ValueError(_("Adresse {ip} liegt nicht im Netz des Interfaces ({v2}).")
                .format(ip=ip, v2=', '.join(map(str, nets))))
        if not is_free(ip, used):
            raise ValueError(_("Adresse {ip} ist schon vergeben.").format(ip=ip))
        out.append(f"{ip}/{ip.max_prefixlen}")
    return out


def validate_name(name: str) -> str:
    name = name.strip()
    if not CLIENT_NAME_RE.match(name):
        raise ValueError(_("Name: 1–40 Zeichen, Buchstaben, Ziffern, Leerzeichen und . _ @ + -"))
    return name


def validate_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    m = ENDPOINT_RE.match(endpoint)
    if not m or not 1 <= int(m.group(2)) <= 65535:
        raise ValueError(_("Endpoint: Hostname:Port, z. B. vpn.example.org:51820"))
    return endpoint


def validate_dns(dns: str) -> list[str]:
    values = [v.strip() for v in dns.replace(";", ",").split(",") if v.strip()]
    for v in values:
        try:
            ipaddress.ip_address(v)
        except ValueError:
            raise ValueError(_("DNS: {v} ist keine IP-Adresse.").format(v=v)) from None
    return values


@dataclass(frozen=True)
class ClientSpec:
    """Validated dialog input."""

    name: str
    addresses: list[str]  # Client VPN address(es), /32 or /128
    endpoint: str  # Externally reachable server, host:port
    full_tunnel: bool  # True: route all traffic through VPN; otherwise only VPN networks
    dns: list[str] = field(default_factory=list)
    psk: bool = True
    keepalive: bool = True


def server_peer_block(name: str, public_key: str, addresses: list[str], psk: str | None) -> str:
    """Build a [Peer] block for the server config; with PSK, send only via stdin and never log it."""
    if not is_valid_key(public_key):
        raise ValueError(_("ungültiger PublicKey"))
    lines = [f"# {validate_name(name)}", "[Peer]", f"PublicKey = {public_key}"]
    if psk is not None:
        lines.append(f"PresharedKey = {psk}")
    lines.append(f"AllowedIPs = {', '.join(addresses)}")
    return "\n".join(lines) + "\n"


def preview_peer_block(name: str, public_key: str, addresses: list[str], psk: bool) -> str:
    """Like server_peer_block, but redact the PSK for diff display and tests."""
    text = server_peer_block(name, public_key, addresses, None)
    if psk:
        text = text.replace("\nAllowedIPs", "\nPresharedKey = (neu, wird nicht angezeigt)\nAllowedIPs", 1)
    return text


def client_allowed_ips(spec: ClientSpec, interface_addresses: list[str]) -> list[str]:
    if spec.full_tunnel:
        return FULL_TUNNEL
    return [str(n) for n in interface_networks(interface_addresses)]


def client_config(spec: ClientSpec, private_key: str, server_public_key: str, psk: str | None,
                  interface_addresses: list[str]) -> str:
    """Build the full client wg-quick config (secret: private key and possibly PSK)."""
    lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {', '.join(spec.addresses)}"]
    if spec.dns:
        lines.append(f"DNS = {', '.join(spec.dns)}")
    lines += ["", "[Peer]", f"PublicKey = {server_public_key}"]
    if psk is not None:
        lines.append(f"PresharedKey = {psk}")
    lines += [f"AllowedIPs = {', '.join(client_allowed_ips(spec, interface_addresses))}",
              f"Endpoint = {spec.endpoint}"]
    if spec.keepalive:
        lines.append(f"PersistentKeepalive = {KEEPALIVE}")
    return "\n".join(lines) + "\n"


def default_endpoint(ssh_target: str, listen_port: int | None) -> str:
    """Suggest user@host → host:ListenPort from the SSH target; aliases or NAT may need edits."""
    host = ssh_target.rsplit("@", 1)[-1]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{listen_port}" if listen_port else host


def overlapping(allowed_ips: Iterable[str], others: dict[str, list[str]]) -> str | None:
    """Find the first peer whose AllowedIPs overlap `allowed_ips`. WireGuard routes each address to exactly
    one peer (wg(8), cryptokey routing); overlap would divert traffic from another peer."""
    mine = [ipaddress.ip_network(a, strict=False) for a in allowed_ips]
    for label, ips in others.items():
        for text in ips:
            try:
                other = ipaddress.ip_network(text, strict=False)
            except ValueError:
                continue
            for net in mine:
                if net.version == other.version and net.overlaps(other):
                    return f"{label} ({other})"
    return None


def is_host_in(address: str, networks: list[Network]) -> bool:
    """Check for a single address (/32 or /128) in a network: a client's VPN address."""
    try:
        net = ipaddress.ip_network(address, strict=False)
    except ValueError:
        return False
    return net.prefixlen == net.max_prefixlen and any(net.version == n.version and net.subnet_of(n)
                                                      for n in networks)
