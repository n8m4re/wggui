#!/bin/sh
# Launch wireguard-gui from the repo's .venv, creating it on first run. Works on Linux distros
# with uv (https://docs.astral.sh/uv/) and Python ≥ 3.12.
#
# Qt source:
#   system – use distro PySide6 via --system-site-packages (matches desktop style)
#   pypi   – otherwise install PySide6-Essentials from PyPI in .venv ("qt" extra in pyproject.toml)
#
# Usage: scripts/wggui.sh [--neu] [app arguments]
#   --neu              remove and recreate .venv
#   WGGUI_PYTHON=…     use a specific interpreter (e.g. /usr/bin/python3.13)
#   WGGUI_QT=pypi      force PyPI Qt even if the distro provides PySide6
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
venv="$repo/.venv"
mark="$venv/.wggui-qt"

command -v uv >/dev/null 2>&1 || {
    echo "Fehlt: uv – Installation: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
}

version_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; }
has_pyside() { "$1" -c 'import PySide6.QtWidgets' 2>/dev/null; }

# Select an interpreter with system PySide6 first, otherwise the first compatible version
py="" qt=pypi
for name in ${WGGUI_PYTHON:-python3.14 python3.13 python3.12 python3}; do
    path=$(command -v "$name" 2>/dev/null) || continue
    version_ok "$path" || continue
    if [ "${WGGUI_QT:-}" != pypi ] && has_pyside "$path"; then
        py=$path qt=system
        break
    fi
    [ -n "$py" ] || py=$path
done

if [ "${1:-}" = "--neu" ]; then
    rm -rf -- "$venv"
    shift
fi
if [ -f "$mark" ] && [ "$(cat "$mark")" != "$qt" ]; then
    echo "Qt-Quelle geändert ($(cat "$mark") → $qt): lege .venv neu an"
    rm -rf -- "$venv"
fi

cd "$repo"
if [ ! -x "$venv/bin/python" ]; then
    if [ "$qt" = system ]; then
        echo "Lege .venv an: $py mit PySide6 aus der Distro"
        uv venv --quiet --system-site-packages --python "$py" "$venv"
    else
        echo "Kein PySide6 aus der Distro gefunden → PySide6-Essentials aus PyPI (einmalig ca. 100 MB)"
        echo "Tipp: Mit dem Distro-Paket (z. B. Fedora: python3-pyside6) passt die App besser zum Desktop-Stil."
        uv venv --quiet --python "${py:->=3.12}" "$venv"
    fi
    echo "$qt" > "$mark"
fi

# Sync dependencies from uv.lock (fast when already up to date)
if [ "$(cat "$mark" 2>/dev/null || echo system)" = pypi ]; then
    uv sync --frozen --quiet --extra qt
else
    uv sync --frozen --quiet
fi
"$venv/bin/python" -c 'import PySide6.QtWidgets' 2>/dev/null || {
    echo "PySide6 in der .venv nicht nutzbar – mit '$0 --neu' neu anlegen" >&2
    exit 1
}
exec "$venv/bin/wggui" "$@"
