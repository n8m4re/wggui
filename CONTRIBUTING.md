# Contributing to WGGUI

Thanks for your interest. Issues and pull requests are welcome. Accepted changes are applied by the maintainer and
appear in one of the next sync commits, so your pull request may be closed without being merged directly.

## License

WGGUI is licensed under `GPL-3.0-or-later`. By submitting a contribution, you agree that it is licensed under the
same terms. Contribute only code and assets that you wrote yourself or that come with a GPL-3.0-compatible
license.

- New runtime dependency: it must be GPL-3.0-compatible. Run `python3 scripts/check_licenses.py` and add it to
  [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).
- Copied code or artwork: name the source and the license in `THIRD-PARTY-LICENSES.md`. Do not copy logos or
  other trademarks.

## Language

- Code comments, docstrings, documentation, commit messages and pull requests are in **English**.
- UI strings are the exception. They are written in German in the source and serve as translation keys: wrap them in
  `_()` or `ngettext()` from `wggui.i18n`. Then run `python3 scripts/i18n.py extract`, fill in every catalog in
  `src/wggui/locale/` and make sure `python3 scripts/i18n.py check` passes. See [docs/i18n.md](docs/i18n.md).

## Data in examples and tests

Use synthetic data only: `example.org`, `10.9x.0.0/24` or RFC 5737 networks, and keys from `tests/keys.py` or keys
generated in a test container. Never include real host names, addresses, names, logs, databases or keys.

## Safety rules for server code

- Server private keys and preshared keys never leave the server. Anything read from a server is redacted on the
  server. No key may appear in the database, logs, command arguments or tests.
- Commands are argument lists. Shell scripts are fixed constants that take values only as positional
  parameters. Scripts that run on servers use only POSIX `sh` and `awk`.
- Writes to a server always go through a plan: diff, backup, rollback timer, write, `wg syncconf`, confirmation.
- WGGUI does not change firewall or sysctl settings.

Background: [docs/architecture.md](docs/architecture.md).

## Checks

```bash
uv run --extra qt pytest -q           # unit and GUI tests (offscreen)
uv run --extra qt pytest -m integration   # SSH against distro containers (Docker)
uv run ruff check .
python3 scripts/i18n.py check
python3 scripts/check_licenses.py
```
