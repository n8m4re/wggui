#!/bin/sh
# Build wireguard-gui as a Flatpak and install it for the current user.
# Source: docs.flatpak.org (flatpak-builder, “Building your first Flatpak”, Single-file bundles).
#
# Usage: scripts/flatpak-build.sh [--bundle]
#   --bundle   also create build-flatpak/wggui.flatpak for distribution or installation
# Prerequisites (once, for the current user):
#   flatpak install --user flathub org.flatpak.Builder org.kde.Sdk//6.11 org.kde.Platform//6.11 io.qt.PySide.BaseApp//6.11
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
app=io.github.n8m4re.WGGUI
manifest="$repo/packaging/flatpak/$app.yml"
out="$repo/build-flatpak"

if command -v flatpak-builder >/dev/null 2>&1; then
    builder="flatpak-builder"
elif flatpak info --user org.flatpak.Builder >/dev/null 2>&1 || flatpak info org.flatpak.Builder >/dev/null 2>&1; then
    builder="flatpak run --filesystem=$repo org.flatpak.Builder"
else
    echo "Fehlt: flatpak-builder – flatpak install --user flathub org.flatpak.Builder" >&2
    exit 1
fi

python3 "$repo/scripts/flatpak_deps.py" --check >/dev/null || python3 "$repo/scripts/flatpak_deps.py"

mkdir -p "$out"
# --user --install: install for the current user; --force-clean: clean build directory
# shellcheck disable=SC2086
$builder --user --install --force-clean --state-dir="$out/.flatpak-builder" --repo="$out/repo" \
    "$out/build" "$manifest"

if [ "${1:-}" = "--bundle" ]; then
    flatpak build-bundle "$out/repo" "$out/wggui.flatpak" "$app" \
        --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo
    echo "Bundle: $out/wggui.flatpak"
fi
echo "Starten: flatpak run $app"
