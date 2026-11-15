# Troubleshooting

## Where can I see what happened?

| Location | Contents |
|---|---|
| Log window (**Ctrl+L**, toolbar “Log” or “Show log” button in the message) | Lines from this session, warnings in orange, errors in red |
| `~/.local/state/wireguard-gui/wggui.log` | Persistent rotating log (`wggui.log.1` … `.3`); accessible through “Open log file” in the log window |
| Message bar at the top of the host view | Latest host error with guidance and a “Show log” button |

For more details, `WGGUI_LOG_LEVEL=DEBUG scripts/wggui.sh` logs complete ssh calls and response lengths. Another location: `WGGUI_LOG=/path/file.log`.

All key-like values in the log are masked as `‹key›`. Hostnames and commands appear in plain text; review the log before sharing it.

## Common messages

| Message (log/host view) | Cause | Solution |
|---|---|---|
| `→ 255 … Permission denied (publickey)` | Key not in the agent or not registered for the target user | `ssh-add -l`; add the key to the target user's `authorized_keys`. For a separate key per host: **Host → Show SSH public key…** and add that line |
| `Host key verification failed` | Unknown host key | Run `ssh <target>` once in the terminal and verify the fingerprint |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | Host key changed | Verify, then run `ssh-keygen -R <host>` |
| `Could not resolve hostname` | Wrong name/DNS | Check spelling, DNS, and aliases in the ssh config |
| `Connection refused` / `timed out` / `timeout` | sshd down, port, firewall, VPN | Check the port in the host dialog and the network/VPN |
| `sudo: a password is required` | sudo without NOPASSWD | Set up NOPASSWD (`docs/distros.md`) or log in as root |
| Notice `No root access` | Login user is not root, sudo disabled | Enable “Run commands with sudo (login is not root)” |
| Notice `wireguard-tools not installed – …` | Package missing | Run the displayed command on the server |
| `The config changed since it was read` (rc 3) | File changed between reading and writing (manually or by another tool) | Nothing was written; reload the host and create the client again |
| `wg syncconf failed – the previous config was restored` (rc 4) | Runtime update failed (e.g. `wg-quick strip` failed) | File and tunnel are as before; check `wg-quick strip /etc/wireguard/<if>.conf` on the server |
| `Written but not confirmed` | Confirmation did not arrive (connection lost, timeout) | The server restores the backup after 120 s; then reload. Backup: `/etc/wireguard/<if>.conf.wggui-<tag>` |
| `No pending change – the rollback has probably already run.` (rc 5) | Confirmation arrived too late | The change was rolled back; create it again |
| `New config not created (peer not found?)` (rc 7) | Peer was changed/removed on the server in the meantime | Nothing was changed; “Sync with server…” |
| `setsid missing` (rc 6) / plan: “missing setsid” | `util-linux` is missing on the server | Install the package; the app will not write without a rollback timer |
| `CRITICAL Unhandled error` + traceback | Program error | Open an issue with a log excerpt (check it for private data first) |

Exit codes: `0` OK, `255` error from ssh itself, `127` command not found; other values come from the command on the server.
