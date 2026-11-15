# Supported distros

## Target servers (managed over SSH)

| Family | Examples (`ID`/`ID_LIKE` from os-release(5)) | Installation per wireguard.com/install | Tested (`tests/integration`) |
|---|---|---|---|
| debian | Debian, Ubuntu, Raspbian | `apt install wireguard` | Debian 13 (root, dash/mawk), Ubuntu 24.04 (sudo) |
| fedora | Fedora | `dnf install wireguard-tools` | Fedora 44 (root, bash/gawk) |
| rhel | RHEL, CentOS Stream, Rocky, Alma | `dnf install wireguard-tools`¹ | AlmaLinux 9 (root), AlmaLinux 10 (sudo) – RHEL 9/10 compatible, including a running tunnel |
| arch | Arch, Manjaro | `pacman -S wireguard-tools` | – |
| alpine | Alpine | `apk add -U wireguard-tools` | Alpine 3.22 (root, BusyBox, without systemd) |
| suse | openSUSE Leap/Tumbleweed, SLES | `zypper install wireguard-tools` | – |
| gentoo | Gentoo | `emerge wireguard-tools` | – |
| void | Void | `xbps-install -S wireguard-tools` | – |

¹ RHEL 9+ is missing from wireguard.com/install (checked 2026-11-15). Verified on running systems: `wireguard-tools` comes from the AppStream repository on AlmaLinux 9 (1.0.20210914) and 10 (1.0.20250521) and the command works there (2026-11-15). On real RHEL, AppStream requires an active subscription.

Server requirements: `sshd`, POSIX `sh` and `awk`, `wireguard-tools`. systemd is optional; without systemd, only the list of `wg-quick@` units is absent from the display.

### Login without root (sudo)

On many distros (e.g. Ubuntu), SSH root login is disabled or uncommon. Enable **“Run commands with sudo (login is not root)”** in the host dialog. The app calls `sudo -n -- <befehl>` (sudo(8): `-n` never prompts for a password). This requires NOPASSWD for the user, for example via `/etc/sudoers.d/wggui` with `visudo -f`:

```
admin ALL=(root) NOPASSWD: ALL
```

This is effectively equivalent to root login because the app needs `sh -c` with fixed scripts. Restricting it to individual commands is therefore not practical. Without NOPASSWD, the app shows “sudo asks for a password: set up NOPASSWD for this user or log in as root.”

## Workstation (where the app runs)

| What | Requirement |
|---|---|
| Python | ≥ 3.12 (`requires-python`) |
| uv | https://docs.astral.sh/uv/ |
| Qt | Prefer distro PySide6 (Fedora: `python3-pyside6`); otherwise `scripts/wggui.sh` downloads PySide6-Essentials from PyPI into `.venv` |
| ssh | OpenSSH client, keys in the SSH agent, host keys in `known_hosts` |

`scripts/wggui.sh` looks for `python3.14`, `python3.13`, `python3.12`, `python3`, preferring one with system PySide6. Override with `WGGUI_PYTHON=/path/to/python` and `WGGUI_QT=pypi`.

Tested: Fedora 44 with system PySide6 (Python 3.14) and PyPI PySide6 6.12. Python 3.12 has only been checked statically (ruff `target-version = py312`), not at runtime.
