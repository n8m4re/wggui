"""Write to the server (from stage 2), always following a plan (safety rules in CONTRIBUTING.md):

    Check (file unchanged?) → backup → rollback timer → write → wg syncconf → confirm

- `prepare` only reads: config checksum, interface status, and availability of setsid/wg-quick.
- `apply` builds the new config on the host: append (block via stdin, possibly containing a
  PSK, never in argv/log), edit (awk from edit.py, request via stdin), or copy (`wg syncconf` only).
  The new file must contain [Interface] before replacing anything.
  First, create backup `<if>.conf.wggui-<tag>` and a timer (`setsid sh -c 'sleep N; …'`) that
  restores it if the app cannot confirm (for example, after losing its own connection).
- `confirm` removes the marker so the timer does nothing.

Running tunnels: `wg-quick strip <datei>` → temp file (0600, in config directory) → `wg syncconf <if> <datei>`.
Avoid a pipeline: without pipefail, a failed strip could apply an empty config and remove all
peers and the private key (wg(8) syncconf applies the file exactly).
Sources: wg(8) (syncconf, show), wg-quick(8) (strip CONFIG_FILE), setsid(1) (util-linux and BusyBox),
sha256sum(1) (coreutils; BusyBox).
POSIX sh only (dash, BusyBox ash); see CONTRIBUTING.md (safety rules for server code).
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from wggui.discover import WG_DIR
from wggui.i18n import N_, _
from wggui.parse import IFNAME_RE
from wggui.remote import CommandError, Runner

log = logging.getLogger(__name__)

ROLLBACK_SECONDS = 120
TAG_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# $1 interface, $2 config directory
PREPARE = r"""
f="$2/$1.conf"
[ -f "$f" ] || { echo "Keine Config-Datei $f" >&2; exit 2; }
h=$(sha256sum "$f") || exit 3
echo "sha256 ${h%% *}"
if wg show "$1" >/dev/null 2>&1; then echo "running yes"; else echo "running no"; fi
if command -v setsid >/dev/null 2>&1; then echo "setsid yes"; else echo "setsid no"; fi
if command -v wg-quick >/dev/null 2>&1; then echo "wgquick yes"; else echo "wgquick no"; fi
"""

# Runs detached (setsid) even after the SSH session ends.
# $1 seconds, $2 marker, $3 backup, $4 config, $5 interface, $6 config directory
ROLLBACK = r"""
sleep "$1"
rm "$2" 2>/dev/null || exit 0
cp -p "$3" "$4"
if wg show "$5" >/dev/null 2>&1; then
    t=$(mktemp "$6/.wggui-strip.XXXXXX") || exit 1
    wg-quick strip "$4" > "$t" && [ -s "$t" ] && wg syncconf "$5" "$t"
    rm -f "$t"
fi
"""

# Builders create a new config from the old one ($1 = path) on stdout; extra arguments start at $2.
# Append: stdin = new [Peer] block (secret, PSK).
BUILD_APPEND = r"""cat "$1"; printf '\n'; cat"""
# Keep the file unchanged (sync runtime state to it).
BUILD_COPY = r"""cat "$1" """
# Edit: $2 = awk program (edit.EDIT_AWK), stdin = request (no secrets).
BUILD_EDIT = r"""awk "$2" /dev/stdin "$1" """

# stdin: builder input.
# $1 interface, $2 config directory, $3 expected sha256, $4 tag, $5 rollback seconds, $6 rollback script,
# $7 builder script, $8 onward: builder arguments
APPLY = r"""
set -eu
umask 077
i=$1; d=$2; f="$2/$1.conf"; bak="$f.wggui-$4"; new="$f.wggui-new"; mark="$2/.wggui-pending-$1-$4"
want=$3; secs=$5; rollback=$6; build=$7
shift 7
h=$(sha256sum "$f"); h=${h%% *}
if [ "$h" != "$want" ]; then
    echo "Die Config wurde seit dem Lesen geändert. Neu lesen und erneut versuchen." >&2
    exit 3
fi
command -v setsid >/dev/null 2>&1 || { echo "setsid fehlt – ohne Rollback-Timer wird nichts geschrieben" >&2; exit 6; }
cp -p "$f" "$new"
if ! sh -c "$build" sh "$f" "$@" > "$new"; then
    rm -f "$new"
    echo "Neue Config nicht erzeugt (Peer nicht gefunden?) – nichts geändert" >&2
    exit 7
fi
if ! grep -qi '^[[:space:]]*\[interface\]' "$new"; then
    rm -f "$new"
    echo "Neue Config ohne [Interface] – nichts geändert" >&2
    exit 7
fi
cp -p "$f" "$bak"
echo "$bak" > "$mark"
setsid sh -c "$rollback" sh "$secs" "$mark" "$bak" "$f" "$i" "$d" </dev/null >/dev/null 2>&1 &
mv "$new" "$f"
sync_conf() {
    t=$(mktemp "$d/.wggui-strip.XXXXXX") || return 1
    if wg-quick strip "$1" > "$t" && [ -s "$t" ] && wg syncconf "$i" "$t"; then rm -f "$t"; return 0; fi
    rm -f "$t"
    return 1
}
if wg show "$i" >/dev/null 2>&1; then
    if ! sync_conf "$f"; then
        cp -p "$bak" "$f"
        rm -f "$mark"
        sync_conf "$f" || true
        echo "wg syncconf fehlgeschlagen – alte Config wiederhergestellt" >&2
        exit 4
    fi
    echo "synced"
else
    echo "file-only"
fi
echo "backup $bak"
"""

# $1 interface, $2 config directory, $3 tag
CONFIRM = r"""
rm "$2/.wggui-pending-$1-$3" 2>/dev/null || {
    echo "Keine offene Änderung – der Rollback ist vermutlich schon gelaufen" >&2
    exit 5
}
echo "confirmed"
"""


# Host script exit codes (APPLY/CONFIRM) → clear, translated message
RC_MESSAGES = {
    3: N_("Die Config wurde seit dem Lesen geändert – nichts geschrieben. Neu lesen (F5) und erneut versuchen."),
    4: N_("wg syncconf fehlgeschlagen – die alte Config wurde wiederhergestellt."),
    5: N_("Keine offene Änderung – der Rollback ist vermutlich schon gelaufen."),
    6: N_("Auf dem Host fehlt setsid – ohne Rollback-Timer wird nichts geschrieben."),
    7: N_("Neue Config nicht erzeugt (Peer nicht gefunden?) – nichts geändert."),
}


class HostScriptError(CommandError):
    """CommandError with a clear message; stderr remains in `result` and the log."""

    def __init__(self, result, message: str):  # noqa: ANN001 – remote.Result
        Exception.__init__(self, message)
        self.result = result

    @classmethod
    def wrap(cls, err: CommandError) -> CommandError:
        text = RC_MESSAGES.get(err.result.returncode)
        return cls(err.result, _(text)) if text else err


@dataclass(frozen=True)
class Prepared:
    conf_hash: str
    running: bool
    setsid: bool
    wg_quick: bool


def _check_iface(iface: str) -> None:
    if not IFNAME_RE.match(iface):
        raise ValueError(f"ungültiger Interface-Name: {iface!r}")


def prepare(runner: Runner, iface: str, conf_dir: str = WG_DIR) -> Prepared:
    """Read-only checksum and tool checks for the write plan."""
    _check_iface(iface)
    out = runner.run(["sh", "-c", PREPARE, "sh", iface, conf_dir]).stdout
    values = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    conf_hash = values.get("sha256", "")
    if not HASH_RE.match(conf_hash):
        raise ValueError(_("Prüfsumme der Config nicht lesbar"))
    return Prepared(conf_hash, values.get("running") == "yes", values.get("setsid") == "yes",
                    values.get("wgquick") == "yes")


def make_tag(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


@dataclass(frozen=True)
class WritePlan:
    """Planned write. `block` is secret (PSK) → repr=False; display `preview`."""

    host_name: str
    iface: str
    conf_dir: str
    preview: str  # Display diff (lines prefixed with “+ ”/“- ”); never secret
    block: str = field(repr=False)  # Builder stdin; secret (PSK) when appending
    conf_hash: str = ""
    running: bool = False
    tag: str = ""
    rollback_seconds: int = ROLLBACK_SECONDS
    warnings: tuple[str, ...] = ()
    builder: str = BUILD_APPEND
    builder_args: tuple[str, ...] = ()
    action: str = N_("Peer-Block anhängen")  # Display via _() in steps()/PlanDialog

    @property
    def conf_path(self) -> str:
        return f"{self.conf_dir}/{self.iface}.conf"

    @property
    def backup_path(self) -> str:
        return f"{self.conf_path}.wggui-{self.tag}"

    def steps(self) -> list[str]:
        sync = (_("wg syncconf: laufender Tunnel übernimmt die Änderung, bestehende Verbindungen bleiben")
                if self.running else _("Interface läuft nicht: nur die Datei wird geändert, gilt beim nächsten Start"))
        return [
            _("Prüfen, ob {conf_path} seit dem Lesen unverändert ist").format(conf_path=self.conf_path),
            _("Backup: {backup_path}").format(backup_path=self.backup_path),
            _("Rollback-Timer: ohne Bestätigung nach {rollback_seconds} s Backup zurückspielen")
                .format(rollback_seconds=self.rollback_seconds),
            _("{action}: {conf_path} (neue Datei wird vorher geprüft)")
                .format(action=_(self.action), conf_path=self.conf_path),
            sync,
            _("Bestätigen (neuer SSH-Aufruf) – erst dann bleibt die Änderung"),
        ]


def check_plan(plan: WritePlan, prepared: Prepared) -> None:
    """Check that the plan can run before display; otherwise raise ValueError with guidance."""
    _check_iface(plan.iface)
    if not TAG_RE.match(plan.tag) or not HASH_RE.match(plan.conf_hash):
        raise ValueError(_("interner Fehler: Plan unvollständig"))
    if not prepared.setsid:
        raise ValueError(_("Auf dem Host fehlt setsid (util-linux) – ohne Rollback-Timer schreibt die App nicht."))
    if prepared.running and not prepared.wg_quick:
        raise ValueError(_("Auf dem Host fehlt wg-quick – der laufende Tunnel kann nicht aktualisiert werden."))


def apply(runner: Runner, plan: WritePlan) -> str:
    """Run the plan; return “synced” or “file-only”. Failures raise CommandError with stderr."""
    _check_iface(plan.iface)
    if not TAG_RE.match(plan.tag) or not HASH_RE.match(plan.conf_hash):
        raise ValueError(_("Plan unvollständig"))
    seconds = str(int(plan.rollback_seconds))
    log.info("%s: schreibe %s (Backup %s, Rollback %s s)", plan.host_name, plan.conf_path, plan.backup_path, seconds)
    try:
        result = runner.run(["sh", "-c", APPLY, "sh", plan.iface, plan.conf_dir, plan.conf_hash, plan.tag, seconds,
                             ROLLBACK, plan.builder, *plan.builder_args], stdin=plan.block)
    except CommandError as err:
        raise HostScriptError.wrap(err) from err
    mode = "synced" if "synced" in result.stdout.split() else "file-only"
    log.info("%s: %s geschrieben (%s)", plan.host_name, plan.conf_path, mode)
    return mode


def confirm(runner: Runner, plan: WritePlan) -> None:
    try:
        runner.run(["sh", "-c", CONFIRM, "sh", plan.iface, plan.conf_dir, plan.tag])
    except CommandError as err:
        raise HostScriptError.wrap(err) from err
    log.info("%s: Änderung an %s bestätigt", plan.host_name, plan.conf_path)


def ssh_over_interface(remote_ips: Iterable[str], interface_addresses: Iterable[str]) -> bool:
    """Check whether SSH reaches the target IP through this interface; changes may cut off access."""
    nets = []
    for a in interface_addresses:
        try:
            nets.append(ipaddress.ip_interface(a).network)
        except ValueError:
            continue
    for ip in remote_ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if any(addr.version == n.version and addr in n for n in nets):
            return True
    return False



class NotConfirmedError(RuntimeError):
    """Written, but confirmation failed; the host will roll back the change."""


def write_and_confirm(runner: Runner, plan: WritePlan) -> str:
    """Apply and confirm via separate SSH calls; confirmation proves access still works."""
    mode = apply(runner, plan)
    try:
        confirm(runner, plan)
    except Exception as err:
        log.error("%s: Bestätigung fehlgeschlagen: %s", plan.host_name, err)
        raise NotConfirmedError(
            _("Geschrieben, aber nicht bestätigt: {err}\n→ {plan_host_name} spielt nach "
              "{plan_rollback_seconds} s das Backup {plan_backup_path} zurück. Danach neu lesen und "
              "erneut versuchen.")
                  .format(err=err,
                          plan_host_name=plan.host_name,
                          plan_rollback_seconds=plan.rollback_seconds,
                          plan_backup_path=plan.backup_path)) from err
    return mode


# --- Prune backups ------------------------------------------------------------------------------------
# Delete only old config copies, not the config itself; no rollback timer is needed.
# Preview in the app and delete only the listed files. Always keep a backup referenced by an
# active marker (.wggui-pending-*, running rollback timer).

# $1 interface, $2 config directory → one tag per line (JJJJMMTT-hhmmss), ascending
LIST_BACKUPS = r"""
for f in "$2/$1".conf.wggui-[0-9]*; do
    [ -f "$f" ] || continue
    echo "${f##*.wggui-}"
done
"""

# $1 interface, $2 config directory, $3 onward: tags → “removed <tag>” or “kept <tag>” per file
PRUNE_BACKUPS = r"""
i=$1; d=$2
shift 2
for t in "$@"; do
    f="$d/$i.conf.wggui-$t"
    [ -f "$f" ] || continue
    if cat "$d"/.wggui-pending-* 2>/dev/null | grep -qx "$f"; then
        echo "kept $t"
        continue
    fi
    rm -f "$f" && echo "removed $t"
done
"""
KEEP_BACKUPS = 5


def list_backups(runner: Runner, iface: str, conf_dir: str = WG_DIR) -> list[str]:
    """List backup tags, oldest first; read-only."""
    _check_iface(iface)
    out = runner.run(["sh", "-c", LIST_BACKUPS, "sh", iface, conf_dir]).stdout
    return sorted(t for t in out.split() if TAG_RE.match(t))


def backups_to_prune(tags: list[str], keep: int = KEEP_BACKUPS) -> list[str]:
    tags = sorted(t for t in tags if TAG_RE.match(t))
    return tags[:-keep] if keep > 0 and len(tags) > keep else ([] if keep > 0 else tags)


def prune_backups(runner: Runner, iface: str, tags: list[str], conf_dir: str = WG_DIR) -> list[str]:
    """Delete exactly these backups; return tags actually removed."""
    _check_iface(iface)
    tags = [t for t in tags if TAG_RE.match(t)]
    if not tags:
        return []
    out = runner.run(["sh", "-c", PRUNE_BACKUPS, "sh", iface, conf_dir, *tags]).stdout
    removed = [line.split()[1] for line in out.splitlines() if line.startswith("removed ")]
    log.info("%s: %d Backup(s) gelöscht", iface, len(removed))
    return removed
