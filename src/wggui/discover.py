"""Read host state without modifying it.

Keys are redacted on the host *before* transfer:
- Config files: awk replaces PrivateKey/PresharedKey values, including commented and disabled
  lines, plus key-like values in Pre/PostUp/Down (REDACT_CONF).
- `wg show all dump`: awk replaces the private key (interface line) and PSK (peer line) (DUMP_AWK).
- Interface public key: pipe the private key to `wg pubkey` on the host (PUBKEY).
parse.py also discards key values; log.py masks key-like text.

Portable POSIX sh/awk (dash, bash, BusyBox ash/awk, mawk, gawk); no GNU sed `I` flag.
`tolower()` and character classes such as `[[:space:]]` are POSIX awk.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field

from wggui.i18n import _
from wggui.parse import IFNAME_RE, InterfaceConf, RuntimeInterface, is_valid_key, parse_conf, parse_dump
from wggui.remote import Runner

log = logging.getLogger(__name__)

REDACTED = "(redacted)"
WG_DIR = "/etc/wireguard"

LIST_CONFS = 'for f in "$1"/*.conf; do [ -f "$f" ] && basename "$f" .conf; done; true'
TOOLS = "wg wg-quick systemctl"
CHECK_TOOLS = 'for c in ' + TOOLS + '; do command -v "$c" >/dev/null 2>&1 && echo "$c"; done; true'
# Redact PrivateKey/PresharedKey (even commented or disabled: “#wggui-off PresharedKey = …”) and
# key-like values in PreUp/PostUp/PreDown/PostDown (e.g. “wg set %i private-key <(echo …)”).
# Avoid regex intervals {n} in key patterns; older mawk versions lack them.
_KEY_AWK = "[A-Za-z0-9+/]" * 42 + "[AEIMQUYcgkosw048]="
REDACT_CONF = (
    "awk 'BEGIN { k = \"" + _KEY_AWK + "\" } "
    "{ l = tolower($0); "
    "if (l ~ /^[[:space:]]*#*[[:space:]]*(wggui-off[[:space:]]*)?(privatekey|presharedkey)[[:space:]]*=/) "
    'sub(/=.*/, "= ' + REDACTED + '"); '
    "else if (l ~ /^[[:space:]]*#*[[:space:]]*(wggui-off[[:space:]]*)?(preup|postup|predown|postdown)[[:space:]]*=/) "
    'gsub(k, "' + REDACTED + '"); '
    "print }' \"$1\""
)
PUBKEY = (
    "awk 'tolower($0) ~ /^[[:space:]]*privatekey[[:space:]]*=/ "
    '{ v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); sub(/[[:space:]#].*$/, "", v); print v; exit }\' '
    '"$1" | wg pubkey'
)
DUMP_AWK = (
    "awk 'BEGIN{FS=OFS=\"\\t\"} "
    "NF==5{$2=\"" + REDACTED + "\"} NF==9{if($3!=\"(none)\")$3=\"" + REDACTED + "\"} {print}'"
)
REDACT_DUMP = "wg show all dump | " + DUMP_AWK

# Distribution family (os-release(5): ID, ID_LIKE) → install command.
# Source: https://www.wireguard.com/install/ (checked 2026-11-15). RHEL/Alma/Rocky 9+ are not listed there;
# `dnf install wireguard-tools` verified on AlmaLinux 9 and 10 (AppStream), see tests/integration targets alma9/alma10.
_FAMILY_OF = {
    "debian": "debian", "ubuntu": "debian", "raspbian": "debian",
    "fedora": "fedora",
    "rhel": "rhel", "centos": "rhel", "rocky": "rhel", "almalinux": "rhel",
    "arch": "arch",
    "alpine": "alpine",
    "suse": "suse", "opensuse": "suse", "opensuse-leap": "suse", "opensuse-tumbleweed": "suse", "sles": "suse",
    "gentoo": "gentoo",
    "void": "void",
}
INSTALL = {
    "debian": "apt install wireguard",
    "fedora": "dnf install wireguard-tools",
    "rhel": "dnf install wireguard-tools",
    "arch": "pacman -S wireguard-tools",
    "alpine": "apk add -U wireguard-tools",
    "suse": "zypper install wireguard-tools",
    "gentoo": "emerge wireguard-tools",
    "void": "xbps-install -S wireguard-tools",
}


@dataclass
class OsRelease:
    pretty_name: str = "unbekannt"
    id: str = ""
    id_like: list[str] = field(default_factory=list)

    @property
    def family(self) -> str | None:
        for name in (self.id, *self.id_like):
            if family := _FAMILY_OF.get(name):
                return family
        return None


@dataclass
class ConnectionInfo:
    os_name: str
    is_root: bool
    wg_installed: bool
    family: str | None = None
    wg_quick: bool = False
    systemd: bool = False

    @property
    def install_hint(self) -> str:
        return INSTALL.get(self.family or "", _("Paket wireguard-tools über den Paketmanager installieren"))


@dataclass
class HostState:
    info: ConnectionInfo
    confs: dict[str, InterfaceConf] = field(default_factory=dict)
    conf_texts: dict[str, str] = field(default_factory=dict)  # Redacted; basis for diffs (stage 3)
    public_keys: dict[str, str] = field(default_factory=dict)  # Derived from the config
    runtime: dict[str, RuntimeInterface] = field(default_factory=dict)
    active_units: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def interface_names(self) -> list[str]:
        return sorted(set(self.confs) | set(self.runtime))


def read_os_release(text: str) -> OsRelease:
    """Parse shell-like KEY=value lines from os-release(5)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            try:
                parts = shlex.split(value) if value else [""]
            except ValueError:
                parts = [value]
            values[key.strip()] = parts[0] if parts else ""
    return OsRelease(
        pretty_name=values.get("PRETTY_NAME") or values.get("NAME") or "unbekannt",
        id=values.get("ID", "").lower(),
        id_like=values.get("ID_LIKE", "").lower().split(),
    )


def parse_os_release(text: str) -> str:
    """Use PRETTY_NAME, otherwise NAME."""
    return read_os_release(text).pretty_name


def check_connection(runner: Runner) -> ConnectionInfo:
    # Per os-release(5), /usr/lib/os-release is the fallback when /etc/os-release is absent
    release = read_os_release(runner.run(["sh", "-c", "cat /etc/os-release 2>/dev/null || "
                                          "cat /usr/lib/os-release"]).stdout)
    uid = runner.run(["id", "-u"]).stdout.strip()
    tools = set(runner.run(["sh", "-c", CHECK_TOOLS]).stdout.split())
    info = ConnectionInfo(os_name=release.pretty_name, is_root=uid == "0", wg_installed="wg" in tools,
                          family=release.family, wg_quick="wg-quick" in tools, systemd="systemctl" in tools)
    log.info("%s: %s (Familie %s), root=%s, Werkzeuge: %s", runner.label, info.os_name, info.family or "?",
             info.is_root, ", ".join(sorted(tools)) or "keine")
    return info


def _check_ifname(name: str) -> None:
    if not IFNAME_RE.match(name):
        raise ValueError(f"ungültiger Interface-Name: {name!r}")


def read_conf_redacted(runner: Runner, name: str) -> str:
    _check_ifname(name)
    return runner.run(["sh", "-c", REDACT_CONF, "sh", f"{WG_DIR}/{name}.conf"]).stdout


def conf_public_key(runner: Runner, name: str) -> str | None:
    _check_ifname(name)
    out = runner.run(["sh", "-c", PUBKEY, "sh", f"{WG_DIR}/{name}.conf"], check=False).stdout.strip()
    return out if is_valid_key(out) else None


def discover(runner: Runner) -> HostState:
    info = check_connection(runner)
    state = HostState(info=info)
    if not info.is_root:
        state.warnings.append(_("Kein root-Zugriff – /etc/wireguard ist so nicht lesbar. Als root anmelden "
                              "oder beim Host „sudo verwenden“ einschalten (braucht NOPASSWD)."))
        _log_warnings(runner, state)
        return state
    if not info.wg_installed:
        state.warnings.append(_("wireguard-tools nicht installiert – auf dem Host: {info_install_hint}")
            .format(info_install_hint=info.install_hint))

    listing = runner.run(["sh", "-c", LIST_CONFS, "sh", WG_DIR], check=False).stdout.split()
    for name in listing:
        if not IFNAME_RE.match(name):
            state.warnings.append(_("Datei {name}.conf übersprungen (ungültiger Interface-Name)").format(name=name))
            continue
        try:
            text = read_conf_redacted(runner, name)
            state.confs[name] = parse_conf(text)
            state.conf_texts[name] = text
        except ValueError as err:
            state.warnings.append(_("{name}.conf nicht lesbar: {err}").format(name=name, err=err))
            continue
        if info.wg_installed and (key := conf_public_key(runner, name)):
            state.public_keys[name] = key

    if info.wg_installed:
        dump = runner.run(["sh", "-c", REDACT_DUMP], check=False)
        if dump.ok:
            try:
                state.runtime = parse_dump(dump.stdout)
            except ValueError as err:
                state.warnings.append(_("Laufzeitdaten (wg show) nicht lesbar: {err}").format(err=err))
    if info.systemd:
        units = runner.run(["systemctl", "list-units", "--plain", "--no-legend", "--state=active", "wg-quick@*"],
                           check=False)
        state.active_units = [line.split()[0] for line in units.stdout.splitlines() if line.strip()]

    for name, conf in state.confs.items():
        if conf.save_config:
            state.warnings.append(_("{name}: SaveConfig = true – wg-quick überschreibt die Datei beim Stoppen")
                .format(name=name))
        if name not in state.runtime:
            state.warnings.append(_("{name}: Config vorhanden, Interface läuft nicht").format(name=name))
    for name in state.runtime:
        if name not in state.confs:
            state.warnings.append(_("{name}: läuft, aber keine Datei in {WG_DIR} (anderes Werkzeug?)")
                .format(name=name, WG_DIR=WG_DIR))
    log.info("%s: %d Config(s), %d laufende(s) Interface(s), %d Hinweis(e)", runner.label, len(state.confs),
             len(state.runtime), len(state.warnings))
    _log_warnings(runner, state)
    return state


def _log_warnings(runner: Runner, state: HostState) -> None:
    for warning in state.warnings:
        log.warning("%s: %s", runner.label, warning)
