# Architecture

## Overview

```
GUI (ui/main_window.py) ──Background (QThreadPool)──▶ discover.py ──▶ remote.SshRunner ──ssh──▶ Server
        │                                         apply.py ─┘ (writes, stage 2+)          (sh/awk, wg)
        ▼                                                 ▼
   db.Database (SQLite, GUI thread only)          parse.py (config, dump)
        │
   log.py ──▶ ~/.local/state/wireguard-gui/wggui.log  +  log window (ui/log_view.py)
```

| Module | Purpose |
|---|---|
| `remote.py` | Runs `ssh` with `BatchMode`, a shared connection (`ControlMaster`), optional `sudo -n`; logs each command; `explain()` handles common errors |
| `discover.py` | Reads a host: os-release, tools, configs (redacted), public key, `wg show all dump`, systemd units; produces notices |
| `parse.py` | wg-quick and `dump` formats; discards key values |
| `apply.py` | Write plan (stage 2): `prepare` (checksum, read only) → `apply` (backup, rollback timer, append peer block, `wg syncconf`) → `confirm` |
| `client.py` | New client: free address, peer block for the server, config for the client (without Qt/SSH) |
| `edit.py` | Stage 3: awk program that edits, disables (`#wggui-off`), removes, or creates peer blocks – runs on the host and locally on the redacted copy (preview = same diff) |
| `sync.py` | Stage 3: differences between app ↔ file ↔ runtime, choice of direction → DB changes and `PeerOp`s |
| `sshkeys.py` | Optional SSH key per host: checks the path (without reading its contents), generates an ed25519 pair with `ssh-keygen`, reads the public key |
| `provision.py` | Stage 4: install wireguard-tools, create an interface (key generated on the server), server-to-server tunnel (two plans, shared PSK via stdin) |
| `keys.py` | Generates client keys locally (X25519 via `cryptography`, PSK from `secrets`); memory only |
| `db.py` | Hosts, interfaces, peers; migrations via `PRAGMA user_version`; `plan_import`/`apply_import`; `add_peer` |
| `log.py` | `wggui` logger, rotating file (1 MB × 4), masking, `excepthook` |
| `ui/` | Main window, dialogs, log window, workers |

## Security: three layers against key leaks

1. **On the host:** awk redacts `PrivateKey`/`PresharedKey`, including commented lines, before anything returns over SSH. The host generates the public key with `wg pubkey`.
2. **Parser:** `parse.py` stores only whether a key is present.
3. **Log:** `MaskingFormatter` replaces anything that looks like a WireGuard key. This includes public keys, because the text does not identify which keys are public. stdout and stdin are never logged.

4. **New client keys** are generated locally (`keys.py`) and held only in memory: the peer block with PSK goes to the host via **stdin** (never argv or the log); the client config goes only to the QR dialog and, optionally, a file (0600). The DB stores only the public key and whether a PSK is present.

Known limit: A key placed directly in a `PostUp` line (instead of `PrivateKey`) is not redacted on the host. It then enters the database as `extra`; the log masks it. Such configs are uncommon (wg-quick(8) recommends `PrivateKey` or a file).

## Threads

SSH runs in `QThreadPool` tasks. Results arrive by signal in the GUI thread; only that thread uses the database and widgets. Log lines from worker threads also reach the log window by signal.

## Data and storage

| What | Path | Contents |
|---|---|---|
| Database | `$WGGUI_DB`, otherwise `$XDG_DATA_HOME/wireguard-gui/wggui.db` | Hosts (target, port, sudo), interfaces, peers, public keys; no private keys/PSKs |
| Log | `$WGGUI_LOG`, otherwise `$XDG_STATE_HOME/wireguard-gui/wggui.log` (permissions 0600) | Actions, commands with exit code and duration, errors with stderr, tracebacks |
| WireGuard configs | on the server: `/etc/wireguard/<if>.conf` | Read; from stage 2, append peer block (plan) |
| Backups | on the server: `/etc/wireguard/<if>.conf.wggui-<JJJJMMTT-hhmmss>` | State before each change (not deleted automatically) |

Schema versions: 1 (stage 1), 2 (`host.sudo` column), 3 (defaults for new clients: `interface.client_endpoint`, `interface.client_dns`), 4 (`peer.disabled`), 5 (`host.identity_file`, path only). Migrations run automatically when opening the database.

## Stages

Stage 1 only reads. From stage 2, the app writes through a plan (`apply.py`):

| Step | Where | What |
|---|---|---|
| 1. Read | `prepare` | Config sha256, whether the interface is running, whether `setsid`/`wg-quick` are available (read only) |
| 2. Preview | `PlanDialog` | Diff (PSK indicated only), sequence, warnings; “Write to <host>” button, Enter cancels |
| 3. Check | `APPLY` | Same checksum? Otherwise abort (rc 3), nothing written |
| 4. Backup | `APPLY` | `cp -p` to `<if>.conf.wggui-<tag>` |
| 5. Rollback timer | `APPLY` → `ROLLBACK` | `setsid sh -c 'sleep 120; …'` survives the end of the SSH session; restores the backup while the marker `.wggui-pending-<if>-<tag>` exists |
| 6. Write | `APPLY` + builder | New file via builder (`BUILD_APPEND`: append block, `BUILD_EDIT`: awk from `edit.py`, `BUILD_COPY`: unchanged); abort without `[Interface]` or on error (rc 7), then `mv` (permissions preserved) |
| 7. Runtime | `APPLY` | Only if the interface is running: `wg-quick strip <file>` → temporary file → `wg syncconf`. Error or empty output → restore backup (rc 4). No pipe: without pipefail, an empty config could otherwise be passed |
| 8. Confirm | `CONFIRM` | A new SSH call removes the marker; if it fails, the rollback runs |

**Disabled peers** (stage 3): Each line in the block gets the prefix `#wggui-off `. `wg-quick strip` includes comments, but `wg(8)` ignores them – `wg syncconf` removes the peer from runtime. Redaction also recognizes `#wggui-off PresharedKey = …`. With `SaveConfig = true`, wg-quick overwrites the file when stopping, losing disabled peers; the app warns about this.

**Preview = result:** The awk program runs locally on the redacted copy; the integration test checks on every distro that the result on the host (after redaction) is byte for byte identical.

The plan warns if the SSH connection itself runs over the interface (`ssh -G` + local name resolution, `apply.ssh_over_interface`).

## Stage 4: new interfaces and tunnels (`provision.py`)

| Action | How |
|---|---|
| Install wireguard-tools | Fixed package manager command per distribution family (`apt-get`, `dnf`, `pacman`, `apk`, `zypper`), shown before it runs; afterwards `command -v wg` is checked |
| New interface | Name, address and ListenPort are suggested without conflicts. On the server: `umask 077`, `wg genkey`, write `<if>.conf`, rollback timer, then `systemctl enable --now wg-quick@<if>` (without systemd: `wg-quick up`). Only the public key comes back. Without confirmation, the timer stops the interface and removes the file |
| Server-to-server tunnel | One peer block on each side (public key of the other side, endpoint, AllowedIPs, optional keepalive). The preshared key is generated locally and reaches both servers via stdin only. Two plans run one after the other, each with backup and rollback |

Firewall, forwarding and routes stay untouched; the app only shows hints.

