"""Change peers in an existing wg-quick config (stage 3): edit, disable/enable, remove,
add (without PSK), or rekey (new QR code for an existing client).

An awk program changes the config **on the host** (builder `apply.BUILD_EDIT`): private key and PSKs stay
there; the app sends a request via stdin. The same program runs locally on a redacted copy
(`transform_local`), so the preview shows the exact diff the host will produce.

Disable: WireGuard has no disabled state. Comment out each line of the peer block, including its
name comment, with the `#wggui-off ` prefix. `wg-quick strip` ignores comments, so `wg syncconf`
removes the peer from runtime state; enabling removes the prefix.

Block boundaries: a [Peer] section ends at the next section header. Adjacent comment lines
immediately above `[Peer]` belong to that peer (name comment, PiVPN “### begin … ###”).
POSIX awk only (gawk, mawk, BusyBox); see CONTRIBUTING.md (safety rules for server code).
"""

from __future__ import annotations

import difflib
import ipaddress
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from wggui.client import validate_name
from wggui.i18n import _
from wggui.parse import is_valid_key

OFF_PREFIX = "#wggui-off "
OPS = ("edit", "state", "remove", "add", "rekey")
REDACTED = "(redacted)"  # Like discover.REDACTED; replaces the new PSK in previews

# File 1 (host stdin): one request per line
#   op<TAB>key<TAB>name<TAB>allowed<TAB>keepalive<TAB>state<TAB>neuer_key<TAB>psk
#   edit: name (empty = keep old), AllowedIPs, Keepalive (empty = none), state on/off/empty
#   state: disable (off) / enable (on) only; remove; add: append a new block (without PSK)
#   rekey: replace PublicKey, remove old PresharedKey lines, set new PSK (empty = none); keep the rest
#   → A request with PSK is secret: send only via stdin, never log it
# File 2: config. Output: new config on stdout. Exit 3: peer missing; 4: add with existing key.
EDIT_AWK = r"""
function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t\r]+$/, "", s); return s }
function unwrap(line) {
    if (substr(line, 1, OL) == OFF) { wrapped = 1; return substr(line, OL + 1) }
    wrapped = 0
    return line
}
function emit(line, off) { if (off && trim(line) != "") print OFF line; else print line }
function is_comment(line,   t) { t = trim(unwrap(line)); return substr(t, 1, 1) == "#" }
function flush(   i, o, off, u, t, started) {
    if (ispeer && key != "") present[key] = 1
    if (!ispeer || key == "" || !(key in op) || op[key] == "add") {
        for (i = 1; i <= n; i++) print buf[i]
        n = 0
        return
    }
    seen[key] = 1
    o = op[key]
    if (o == "remove") { n = 0; return }
    off = hoff
    if (st[key] == "off") off = 1
    if (st[key] == "on") off = 0
    if (o == "rekey") {
        for (i = 1; i <= n; i++) {
            u = unwrap(buf[i]); t = tolower(trim(u))
            if (t ~ /^presharedkey[ \t]*=/) continue
            if (t ~ /^publickey[ \t]*=/) {
                emit("PublicKey = " nk[key], off)
                if (ps[key] != "") emit("PresharedKey = " ps[key], off)
                continue
            }
            emit(u, off)
        }
        n = 0
        return
    }
    if (o == "state") {
        for (i = 1; i <= n; i++) emit(unwrap(buf[i]), off)
        n = 0
        return
    }
    newname = nm[key]
    if (newname == "") {
        for (i = 1; i < hdr; i++) emit(unwrap(buf[i]), off)
    } else {
        emit("# " newname, off)
    }
    started = 0
    for (i = hdr; i <= n; i++) {
        u = unwrap(buf[i]); t = tolower(trim(u))
        if (t ~ /^[a-z]+[ \t]*=/) started = 1
        if (newname != "" && i > hdr && !started && substr(t, 1, 1) == "#") continue
        if (newname != "" && t ~ /^#+[ \t]*name[ \t]*=/) continue
        if (newname != "" && t ~ /^#+[ \t]*end[ \t]/) continue
        if (t ~ /^allowedips[ \t]*=/ || t ~ /^persistentkeepalive[ \t]*=/) continue
        emit(u, off)
        if (t ~ /^publickey[ \t]*=/) {
            emit("AllowedIPs = " al[key], off)
            if (ka[key] != "") emit("PersistentKeepalive = " ka[key], off)
        }
    }
    n = 0
}
BEGIN { OFF = "#wggui-off "; OL = length(OFF); FS = "\t" }
FILENAME == ARGV[1] {  # nicht NR == FNR: bei leerem Auftrag würde sonst die Config als Auftrag gelesen
    if ($0 == "") next
    k = $2; op[k] = $1; nm[k] = $3; al[k] = $4; ka[k] = $5; st[k] = $6; nk[k] = $7; ps[k] = $8
    nops++; order[nops] = k
    next
}
{
    raw = $0
    u = unwrap(raw)
    hw = wrapped
    t = tolower(trim(u))
    if (t ~ /^\[(peer|interface)\]/) {
        nlead = 0
        while (n > 0 && is_comment(buf[n])) { lead[++nlead] = buf[n]; n-- }
        flush()
        n = 0
        for (j = nlead; j >= 1; j--) buf[++n] = lead[j]
        buf[++n] = raw
        hdr = n
        hoff = hw
        ispeer = (t ~ /^\[peer\]/)
        key = ""
        next
    }
    if (ispeer && t ~ /^publickey[ \t]*=/) {
        v = u; sub(/^[^=]*=[ \t]*/, "", v); sub(/[ \t#].*$/, "", v); key = v
    }
    buf[++n] = raw
}
END {
    flush()
    bad = 0
    for (i = 1; i <= nops; i++) {
        k = order[i]
        if (op[k] == "add") {
            if (k in present) { print "Peer existiert schon: " k > "/dev/stderr"; bad = 4; continue }
            print ""
            if (nm[k] != "") print "# " nm[k]
            print "[Peer]"
            print "PublicKey = " k
            print "AllowedIPs = " al[k]
            if (ka[k] != "") print "PersistentKeepalive = " ka[k]
        } else if (op[k] == "rekey" && (nk[k] in present)) {
            print "Neuer Schlüssel ist schon vergeben: " nk[k] > "/dev/stderr"
            bad = 4
        } else if (!(k in seen)) {
            print "Peer nicht gefunden: " k > "/dev/stderr"
            bad = 3
        }
    }
    if (bad) exit bad
}
"""


@dataclass(frozen=True)
class PeerOp:
    op: str
    public_key: str
    name: str = ""
    allowed_ips: tuple[str, ...] = ()
    keepalive: int | None = None
    state: str = ""  # "on", "off", or "" (unchanged)
    new_key: str = ""  # rekey: new PublicKey
    psk: str = field(default="", repr=False)  # rekey: new PresharedKey (secret)

    def line(self, preview: bool = False) -> str:
        """Build a validated request line (no tabs or newlines; values already checked).
        preview=True replaces the PSK with “(redacted)” for the local preview."""
        if self.op not in OPS:
            raise ValueError(_("unbekannte Operation {op!r}").format(op=self.op))
        if not is_valid_key(self.public_key):
            raise ValueError(_("ungültiger PublicKey"))
        name = validate_name(self.name) if self.name else ""
        allowed = [str(ipaddress.ip_network(a, strict=False)) for a in self.allowed_ips]
        if self.op in ("edit", "add") and not allowed:
            raise ValueError(_("AllowedIPs fehlen."))
        if self.keepalive is not None and not 0 < self.keepalive <= 65535:
            raise ValueError(_("Keepalive: 1–65535 Sekunden oder leer."))
        if self.state not in ("", "on", "off") or (self.op == "state" and not self.state):
            raise ValueError(_("Zustand: on oder off"))
        if self.op == "rekey":
            if not is_valid_key(self.new_key) or self.new_key == self.public_key:
                raise ValueError(_("neuer PublicKey ungültig"))
            if self.psk and not is_valid_key(self.psk):
                raise ValueError(_("ungültiger PresharedKey"))
        keep = "" if self.keepalive is None else str(self.keepalive)
        psk = (REDACTED if self.psk else "") if preview else self.psk
        return "\t".join([self.op, self.public_key, name, ", ".join(allowed), keep, self.state, self.new_key, psk])


def order_text(ops: Iterable[PeerOp], preview: bool = False) -> str:
    """Build the host request for stdin; rekey may include a PSK, so never log or display it."""
    ops = list(ops)
    keys = [o.public_key for o in ops]
    if len(keys) != len(set(keys)):
        raise ValueError(_("Pro Peer nur eine Änderung je Plan."))
    return "".join(o.line(preview) + "\n" for o in ops)


def transform_local(conf_text: str, ops: Iterable[PeerOp]) -> str:
    """Run the host awk program on a redacted local copy for the preview diff.
    New PSKs appear only as “(redacted)”, matching host redaction."""
    order = order_text(ops, preview=True)
    with tempfile.TemporaryDirectory(prefix="wggui-") as tmp:
        order_file, conf_file = Path(tmp) / "auftrag", Path(tmp) / "conf"
        order_file.write_text(order, encoding="utf-8")
        conf_file.write_text(conf_text, encoding="utf-8")
        # Fixed program; values only via files; use awk from PATH, as on the host
        proc = subprocess.run(["awk", EDIT_AWK, str(order_file), str(conf_file)],  # noqa: S603, S607
                              capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        reason = {3: _("Peer nicht gefunden – „Mit Server abgleichen…“ zeigt, was abweicht."),
                  4: _("Peer existiert schon (Schlüssel bereits vergeben).")}.get(proc.returncode)
        raise ValueError(reason or proc.stderr.strip() or f"awk ({proc.returncode})")
    return proc.stdout


def diff_lines(old: str, new: str) -> str:
    """Produce a unified diff without headers for the plan dialog."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2)
    return "\n".join(line for line in lines if not line.startswith(("---", "+++"))) + "\n"
