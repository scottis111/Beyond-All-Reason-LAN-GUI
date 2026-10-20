# Awesome BAR LAN Lobby — Arch Linux

## Files

- `awesome_bar_lan_gui_arch.py` — the lobby application.
- `awesome_bar_lan_gui_arch.sh` — launcher that checks Python/PyQt6 and offers to install PyQt6 using `pacman`.

## Install dependencies

On Arch Linux, open a terminal and run:

```bash
sudo pacman -S --needed python python-pyqt6
```

## Start the lobby

Keep both files in the same directory. From that directory, run:

```bash
chmod +x awesome_bar_lan_gui_arch.sh
./awesome_bar_lan_gui_arch.sh
```

You can also double-click the `.sh` file if your desktop environment is configured to run shell scripts. Running it from a terminal is best for seeing diagnostic messages.

## Configure Beyond All Reason

Open **Settings** in the lobby and confirm the BAR data folder. The usual Linux BAR launcher data directory is:

```text
~/.local/state/Beyond All Reason
```

The selected data folder should contain an `engine/` folder; the selected engine folder should contain `spring` and `libunitsync.so`. If BAR installed its data elsewhere or updated to a new engine version, use **Browse…** or **Detect engine** in Settings.

## Network note

The lobby can add a firewall rule automatically when `ufw` or `firewalld` is installed and configured. A stock Arch system may use `nftables` or have no host firewall enabled, so in that case configure the inbound UDP game port yourself if needed. Internet hosting also usually requires forwarding the chosen UDP port on your router, unless a working VPN port-forwarding setup is used.
