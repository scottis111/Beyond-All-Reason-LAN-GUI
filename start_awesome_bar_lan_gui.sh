#!/usr/bin/env bash
# Linux (Arch) launcher for the Awesome BAR LAN GUI.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/awesome_bar_lan_gui.py"

pause() { read -r -p "Press Enter to close..." _ || true; }

if [ ! -f "$SCRIPT" ]; then
  echo "ERROR: GUI script not found: \"$SCRIPT\""
  pause; exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is not installed. Install it with: sudo pacman -S python"
  pause; exit 1
fi
if ! python3 -c 'import PyQt6.QtWidgets' >/dev/null 2>&1; then
  echo "ERROR: PyQt6 is missing. Install it with: sudo pacman -S python-pyqt6"
  pause; exit 1
fi

python3 "$SCRIPT"
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "The GUI exited with an error (code $status). If an error message appeared above, take a screenshot of it."
  pause
fi
exit $status
