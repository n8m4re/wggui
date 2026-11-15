#!/bin/sh
# Flatpak launcher: package is in /app/lib/python3.13/site-packages (on the runtime's sys.path).
exec python3 -c 'import sys; from wggui.app import main; sys.exit(main())' "$@"
