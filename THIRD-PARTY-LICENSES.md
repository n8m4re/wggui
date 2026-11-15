# Third-party licenses

WGGUI is licensed under the **GNU General Public License v3.0 or later** (`GPL-3.0-or-later`, see [LICENSE](LICENSE)).

This repository contains no copied third-party source code. The dependencies below are installed by your package
manager, `uv` or the Flatpak build. They keep their own licenses, and all of them are compatible with
GPL-3.0-or-later. `python3 scripts/check_licenses.py` checks this against the installed package metadata.

## Python dependencies

| Component | Used for | License | Source |
|---|---|---|---|
| PySide6, shiboken6 (Qt for Python) | User interface | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | <https://code.qt.io/cgit/pyside/pyside-setup.git> |
| cryptography | X25519 key pairs, preshared keys | Apache-2.0 OR BSD-3-Clause | <https://github.com/pyca/cryptography> |
| cffi | Dependency of cryptography | MIT-0 | <https://github.com/python-cffi/cffi> |
| pycparser | Dependency of cffi | BSD-3-Clause | <https://github.com/eliben/pycparser> |
| segno | QR codes | BSD-3-Clause | <https://github.com/heuer/segno> |

PyPI may install PySide6 as `PySide6-Essentials` (same license). The PyPI wheels of cryptography include a
statically linked OpenSSL (Apache-2.0). Each installed package ships its license text in its `*.dist-info` directory.

## Programs that are called, not distributed

WGGUI starts these programs as separate processes. They are not part of this repository or the Flatpak bundle,
except for the OpenSSH client from the Flatpak runtime:

- OpenSSH client (`ssh`, `ssh-keygen`) on the local machine
- `wg`, `wg-quick` (wireguard-tools), `sudo`, `systemd` and the package manager on the managed servers

## Flatpak bundle

The Flatpak build uses the `org.kde.Platform` runtime and the `io.qt.PySide.BaseApp` base app, which adds PySide6
and shiboken6 to the application. It also bundles the Python wheels listed above, pinned by SHA-256 in
`packaging/flatpak/python3-deps.json`. If you redistribute a bundle, the terms of these licenses apply to it,
including LGPL-3.0 for PySide6 (license text and access to the corresponding source, linked above).

## Artwork

- The application icon (`packaging/flatpak/io.github.n8m4re.WGGUI*.svg`) was created for this project and is licensed
  like the code. It contains no parts of third-party logos.
- Toolbar and dialog icons come from your desktop icon theme at runtime (`QIcon.fromTheme`). They are not
  distributed with WGGUI.
- Screenshots in `docs/images/` show synthetic data only.

## Trademarks

WireGuard is a registered trademark of Jason A. Donenfeld. WGGUI is an independent project and is not affiliated
with or endorsed by the WireGuard project. The name is used only to describe what the software works with
(<https://www.wireguard.com/trademark-policy/>).
