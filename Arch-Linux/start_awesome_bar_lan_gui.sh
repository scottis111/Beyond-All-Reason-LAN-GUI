#!/usr/bin/env bash
# Arch Linux launcher for Awesome BAR LAN Lobby.
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || exit 2
SCRIPT="$SCRIPT_DIR/awesome_bar_lan_gui_arch.py"

pause_if_interactive() {
    if [[ -t 0 ]]; then
        read -r -p "Press Enter to close this window... " _
    fi
}

fail() {
    local code="$1"
    shift
    printf 'ERROR: %s\n' "$*" >&2
    pause_if_interactive
    exit "$code"
}

cd -- "$SCRIPT_DIR" || fail 2 "Could not change to the application folder."

if [[ ! -f "$SCRIPT" ]]; then
    fail 2 "awesome_bar_lan_gui_arch.py was not found beside this launcher. Keep both files in the same folder."
fi

PYTHON_CMD="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
if [[ -z "$PYTHON_CMD" ]]; then
    printf 'Python 3 was not found.\n'
    printf 'On Arch Linux, install it with: sudo pacman -S --needed python\n'
    pause_if_interactive
    exit 3
fi

if ! VERSION_TEXT="$("$PYTHON_CMD" -c 'import sys; print("Using Python", sys.version.split()[0])' 2>&1)"; then
    fail 4 "Python could not start: $VERSION_TEXT"
fi
printf '%s\n' "$VERSION_TEXT"

if ! "$PYTHON_CMD" -c 'import PyQt6' >/dev/null 2>&1; then
    printf 'PyQt6 is required by the lobby application but is not installed.\n'
    if ! command -v pacman >/dev/null 2>&1; then
        fail 5 "pacman was not found. Install PyQt6 using your distribution package manager, then rerun this launcher."
    fi
    if [[ ! -t 0 ]]; then
        fail 5 "Run this launcher in a terminal, or install PyQt6 manually with: sudo pacman -S --needed python-pyqt6"
    fi
    read -r -p "Install PyQt6 now using pacman? [Y/n]: " ANSWER
    case "$ANSWER" in
        n|N|no|NO|No)
            fail 6 "PyQt6 was not installed. The lobby cannot start without it."
            ;;
        *)
            if ! command -v sudo >/dev/null 2>&1; then
                fail 5 "sudo was not found. Install python-pyqt6 with an administrator account."
            fi
            if ! sudo pacman -S --needed python-pyqt6; then
                fail 5 "PyQt6 installation failed. Try: sudo pacman -S --needed python python-pyqt6"
            fi
            ;;
    esac
fi

if ! "$PYTHON_CMD" -c 'import PyQt6' >/dev/null 2>&1; then
    fail 5 "PyQt6 still cannot be imported. Try: sudo pacman -S --needed python python-pyqt6"
fi

"$PYTHON_CMD" "$SCRIPT"
APP_EXIT=$?
if [[ "$APP_EXIT" -ne 0 ]]; then
    printf '\nThe application exited with code %s.\n' "$APP_EXIT"
fi
pause_if_interactive
exit "$APP_EXIT"
