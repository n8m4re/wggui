"""Optional per-host SSH keys.

By default, `ssh` uses keys from the SSH agent and ssh config (~/.ssh/config).
A host may use its own key file; the app passes only the **path** to
`ssh -i <datei> -o IdentitiesOnly=yes` (ssh(1), ssh_config(5)). The app never reads the private key;
it displays only the public part (`<datei>.pub`) for installation on the server.

`ssh-keygen -t ed25519` (ssh-keygen(1)) creates new pairs in the app data directory (0700/0600),
without a passphrase: `ssh` uses BatchMode=yes and cannot prompt for one. For passphrases,
use the SSH agent (default “System”).
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from pathlib import Path

from wggui.i18n import _

log = logging.getLogger(__name__)

COMMENT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def key_dir() -> Path:
    """Use $WGGUI_SSH_DIR, otherwise $XDG_DATA_HOME/wireguard-gui/ssh (app data directory in Flatpak)."""
    if env := os.environ.get("WGGUI_SSH_DIR"):
        return Path(env)
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "wireguard-gui" / "ssh"


def suggest_path(host_name: str) -> Path:
    return key_dir() / f"{host_name}_ed25519"


def check_identity(path: str) -> str | None:
    """Return an error or None; check only existence, file type, and permissions, never contents."""
    if not path:
        return _("Schlüsseldatei fehlt.")
    p = Path(path)
    if not p.is_absolute():
        return _("Schlüsseldatei: vollständigen Pfad angeben.")
    if p.suffix == ".pub":
        return _("Schlüsseldatei: die private Datei wählen (ohne .pub).")
    if not p.is_file():
        return _("Schlüsseldatei {p} nicht gefunden.").format(p=p)
    if stat.S_IMODE(p.stat().st_mode) & 0o077:
        return _("Schlüsseldatei {p} ist für andere lesbar – ssh lehnt sie ab (chmod 600).").format(p=p)
    return None


def public_key_text(path: str) -> str | None:
    """Read the public part (<datei>.pub); only this part is meant for display."""
    pub = Path(f"{path}.pub")
    try:
        text = pub.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if text.startswith(("ssh-", "ecdsa-", "sk-")) else None


def generate(path: Path, comment: str) -> str:
    """Create an ed25519 pair without a passphrase; return the public key and never overwrite files."""
    if not COMMENT_RE.match(comment):
        raise ValueError(_("Kommentar für den Schlüssel ungültig"))
    if path.exists() or Path(f"{path}.pub").exists():
        raise ValueError(_("{path} gibt es schon – anderen Namen wählen oder die vorhandene Datei nehmen.")
            .format(path=path))
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    # ssh-keygen(1): -q quiet, -t type, -N new passphrase (empty), -C comment, -f file
    # argv list; validated values (regex for comment, app data path); use ssh-keygen from PATH like ssh
    proc = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment,  # noqa: S603, S607
                           "-f", str(path)], capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise ValueError(_("ssh-keygen fehlgeschlagen: {v1}").format(v1=proc.stderr.strip()[:200] or proc.returncode))
    log.info("SSH-Schlüsselpaar erzeugt: %s (Public Key: %s.pub)", path, path)
    text = public_key_text(str(path))
    if text is None:
        raise ValueError(_("Public Key nach dem Erzeugen nicht lesbar"))
    return text
