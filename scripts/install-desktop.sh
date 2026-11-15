#!/bin/sh
# Add the source version to the application menu, with the app icon (Desktop Entry Specification and Icon Theme
# Specification, freedesktop.org). Launch via scripts/wggui.sh (creates .venv).
#
#   scripts/install-desktop.sh             # menu entry + icon
#   scripts/install-desktop.sh --desktop   # additionally a launcher on the desktop (xdg-user-dir DESKTOP)
#   scripts/install-desktop.sh --remove    # remove menu entry, icon and desktop launcher
#
# Files: ~/.local/share/applications/wireguard-gui.desktop,
#        ~/.local/share/icons/hicolor/scalable/apps/wireguard-gui.svg (+ -symbolic.svg)
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
exe="$repo/scripts/wggui.sh"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
apps="$data/applications"
icons="$data/icons/hicolor/scalable/apps"
name=wireguard-gui   # = QGuiApplication.desktopFileName() in src/wggui/app.py
desktop_dir=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")

refresh() {
    # Rebuild the menu caches if the tools exist (KDE: kbuildsycoca6). The GTK icon cache is left alone: an
    # existing user cache belongs to other apps, and Qt ignores caches older than the icon directory.
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$apps" || true
    command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--remove" ]; then
    rm -f "$apps/$name.desktop" "$icons/$name.svg" "$icons/$name-symbolic.svg" "$desktop_dir/$name.desktop"
    refresh
    echo "Removed: menu entry, icon, desktop launcher"
    exit 0
fi

mkdir -p "$apps" "$icons"
install -m 644 "$repo/packaging/flatpak/io.github.n8m4re.WGGUI.svg" "$icons/$name.svg"
install -m 644 "$repo/packaging/flatpak/io.github.n8m4re.WGGUI-symbolic.svg" "$icons/$name-symbolic.svg"
cat > "$apps/$name.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=WGGUI
Comment=Manage WireGuard® servers over SSH
Comment[de]=WireGuard®-Server per SSH verwalten
Exec=$exe
Icon=$name
Terminal=false
Categories=Network;
StartupWMClass=$name
DESKTOP
command -v desktop-file-validate >/dev/null && desktop-file-validate "$apps/$name.desktop"
echo "Menu entry: $apps/$name.desktop"

if [ "${1:-}" = "--desktop" ]; then
    mkdir -p "$desktop_dir"
    install -m 755 "$apps/$name.desktop" "$desktop_dir/$name.desktop"   # executable = trusted launcher (Plasma)
    echo "Desktop launcher: $desktop_dir/$name.desktop"
fi
refresh
