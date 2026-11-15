"""Run commands on a host through the workstation's OpenSSH client.

The `ssh` command automatically uses `~/.ssh/config`, the SSH agent, and `known_hosts`
(strict host-key checking remains the default). Options from ssh_config(5):
- BatchMode=yes: never prompt for a password/passphrase or unknown host key.
- ControlMaster/ControlPath/ControlPersist: reuse one connection for several commands.

Rules (CONTRIBUTING.md): commands use argv lists; shell only for fixed scripts with positional parameters.
Log command, exit code, duration, and stderr on errors; never log stdout or stdin.
Non-root login uses `sudo -n` (sudo(8): no password prompt), requiring NOPASSWD on the host.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Protocol

from wggui.i18n import N_, _

# user@host, ssh-config alias, or hostname; no leading “-” (would be parsed as an option)
TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]*$")
SSH_ERROR = 255  # ssh(1): exit 255 means an SSH error (connection or authentication)
SHORT = 160

log = logging.getLogger(__name__)

# Common ssh(1)/sudo(8) messages → German guidance
_HINTS = (
    ("Permission denied (publickey", N_("Anmeldung abgelehnt: Schlüssel nicht im SSH-Agent oder nicht in "
     "authorized_keys des Zielnutzers.")),
    ("Host key verification failed", N_("Host-Key unbekannt oder geändert: einmal im Terminal `ssh <Ziel>` "
     "ausführen und den Fingerabdruck prüfen.")),
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", N_("Host-Key hat sich geändert – erst prüfen, dann den alten "
     "Eintrag mit `ssh-keygen -R <host>` entfernen.")),
    ("Could not resolve hostname", N_("Hostname nicht auflösbar: Schreibweise, DNS oder ssh-Config prüfen.")),
    ("Connection refused", N_("Port geschlossen: läuft sshd, stimmt der Port?")),
    ("Connection timed out", N_("Keine Antwort: Host aus, Firewall oder falsches Netz (VPN?).")),
    ("Zeitüberschreitung", N_("Keine Antwort: Host aus, Firewall oder falsches Netz (VPN?).")),
    ("No route to host", N_("Keine Route zum Host: Netz oder VPN prüfen.")),
    ("a password is required", N_("sudo verlangt ein Passwort: NOPASSWD für diesen Nutzer einrichten "
     "oder als root anmelden.")),
    ("a terminal is required", N_("sudo verlangt ein Passwort: NOPASSWD für diesen Nutzer einrichten "
     "oder als root anmelden.")),
    ("sudo: not found", N_("sudo ist auf dem Host nicht installiert.")),
    ("sudo: command not found", N_("sudo ist auf dem Host nicht installiert.")),
)


COMMAND_FAILED = N_("Befehl fehlgeschlagen")  # Prefix of each CommandError message (ui.main_window.short_error)


def explain(stderr: str) -> str | None:
    """Return guidance for a known error message, otherwise None."""
    for needle, hint in _HINTS:
        if needle in stderr:
            return _(hint)
    return None


def short_command(argv: list[str] | tuple[str, ...]) -> str:
    joined = shlex.join(argv)
    return joined if len(joined) <= SHORT else joined[: SHORT - 1] + "…"


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(RuntimeError):
    def __init__(self, result: Result):
        self.result = result
        detail = result.stderr.strip()[:500] or "(keine Ausgabe)"
        text = f"{_(COMMAND_FAILED)} ({result.returncode}): {short_command(result.argv)}\n{detail}"
        hint = explain(result.stderr)
        super().__init__(f"{text}\n→ {hint}" if hint else text)


class Runner(Protocol):
    label: str

    def run(self, argv: list[str], *, stdin: str | None = None, check: bool = True) -> Result: ...

    def remote_addresses(self) -> list[str]: ...


class SshRunner:
    """Run argv on `target` as the login user (root) or via `sudo -n` when sudo=True."""

    def __init__(self, target: str, port: int | None = None, *, sudo: bool = False,
                 extra_options: list[str] | None = None, timeout: float = 60.0, identity_file: str | None = None):
        if not TARGET_RE.match(target):
            raise ValueError(f"ungültiges SSH-Ziel: {target!r}")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError(f"ungültiger Port: {port}")
        self.target, self.port, self.sudo, self.timeout = target, port, sudo, timeout
        self.label = f"{target}:{port}" if port else target
        self._extra = list(extra_options or [])
        # Per-host key: offer only this identity (ssh(1) -i, ssh_config(5) IdentitiesOnly)
        if identity_file:
            self._extra += ["-i", identity_file, "-o", "IdentitiesOnly=yes"]

    def base_command(self) -> list[str]:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and os.path.isdir(runtime):
            # %C hashes target, port, and user (ssh_config(5), TOKENS) → short, unique socket
            cmd += ["-o", "ControlMaster=auto", "-o", f"ControlPath={runtime}/wggui-%C", "-o", "ControlPersist=60"]
        if self.port:
            cmd += ["-p", str(self.port)]
        return [*cmd, *self._extra, "--", self.target]

    def remote_argv(self, argv: list[str]) -> list[str]:
        # sudo(8): -n never prompts for a password; -- ends sudo options
        return ["sudo", "-n", "--", *argv] if self.sudo else list(argv)

    def remote_addresses(self) -> list[str]:
        """Resolve SSH destination IPs locally: `ssh -G` evaluates ssh config
        (ssh(1) -G, `hostname` key); getaddrinfo resolves it. ProxyJump/ProxyCommand → []."""
        cmd = ["ssh", "-G", *(["-p", str(self.port)] if self.port else []), *self._extra, "--", self.target]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        conf = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
        if conf.get("proxyjump", "none") != "none" or conf.get("proxycommand", "none") != "none":
            return []
        try:
            infos = socket.getaddrinfo(conf.get("hostname", ""), None, type=socket.SOCK_STREAM)
        except OSError:
            return []
        return sorted({str(info[4][0]) for info in infos})

    def run(self, argv: list[str], *, stdin: str | None = None, check: bool = True) -> Result:
        # ssh(1) passes a command string to the remote login shell → quote it once
        full = [*self.base_command(), shlex.join(self.remote_argv(argv))]
        log.debug("%s: ssh-Aufruf: %s", self.label, shlex.join(full))
        start = time.monotonic()
        try:
            proc = subprocess.run(full, input=stdin, capture_output=True, text=True, timeout=self.timeout,
                                  check=False)
            result = Result(tuple(argv), proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            result = Result(tuple(argv), SSH_ERROR, "", f"Zeitüberschreitung nach {self.timeout:.0f}s")
        except OSError as err:  # For example, ssh is not installed
            result = Result(tuple(argv), SSH_ERROR, "", f"ssh nicht startbar: {err}")
        log_result(self.label, result, time.monotonic() - start, check)
        if check and not result.ok:
            raise CommandError(result)
        return result


def log_result(label: str, result: Result, seconds: float, check: bool) -> None:
    """Log one line per command; errors include stderr and guidance. Expected failures (check=False) use INFO."""
    cmd = short_command(result.argv)
    if result.ok:
        log.info("%s: %s → 0 (%.2f s)", label, cmd, seconds)
        log.debug("%s: %d Zeichen Ausgabe", label, len(result.stdout))
        return
    level = logging.WARNING if check or result.returncode == SSH_ERROR else logging.INFO
    detail = result.stderr.strip()[:500] or "(keine Fehlerausgabe)"
    log.log(level, "%s: %s → %d (%.2f s): %s", label, cmd, result.returncode, seconds, detail)
    if hint := explain(result.stderr):
        log.log(level, "%s: Hinweis: %s", label, hint)


@dataclass
class Call:
    argv: list[str]
    stdin: str | None


@dataclass
class FakeRunner:
    """Test double: match quoted commands with regexes and record calls."""

    label: str = "fake"
    rules: list[tuple[re.Pattern[str], str, int, str]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)

    def remote_addresses(self) -> list[str]:
        return list(self.addresses)

    def on(self, pattern: str, stdout: str = "", returncode: int = 0, stderr: str = "") -> FakeRunner:
        self.rules.append((re.compile(pattern, re.S), stdout, returncode, stderr))
        return self

    def run(self, argv: list[str], *, stdin: str | None = None, check: bool = True) -> Result:
        self.calls.append(Call(list(argv), stdin))
        joined = shlex.join(argv)
        for pattern, stdout, rc, stderr in self.rules:
            if pattern.search(joined):
                result = Result(tuple(argv), rc, stdout, stderr or ("" if rc == 0 else "fake error"))
                break
        else:
            result = Result(tuple(argv), 127, "", f"FakeRunner: keine Regel für {joined}")
        log_result(self.label, result, 0.0, check)
        if check and not result.ok:
            raise CommandError(result)
        return result
