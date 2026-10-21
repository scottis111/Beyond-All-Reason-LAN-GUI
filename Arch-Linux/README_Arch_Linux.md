# Awesome BAR LAN Lobby — Arch Linux

A direct-connect host/join launcher for **Beyond All Reason (BAR)**. It lets a host configure a match, share the lobby over TCP, let a connected human player change their own Team, Bonus %, Side, and Color, and launch BAR using generated Spring engine start scripts. It also includes Light and Dark GUI themes.

> **Important:** This is a direct-connect helper, not a matchmaking service or relay. Every computer needs a compatible BAR installation, engine/game version, and the required map/game files. Hosting over the internet normally requires network port forwarding or a compatible VPN port-forwarding setup.

## Contents

Keep these two files in the same directory:

- `awesome_bar_lan_gui_arch.py` — the application.
- `awesome_bar_lan_gui_arch.sh` — the Arch Linux launcher.

The launcher looks for the Python file by this exact filename. If your launcher has a different filename, edit its `SCRIPT=` line to match.

## Requirements

- Arch Linux or a compatible Arch-based distribution.
- Python 3.
- PyQt6.
- Beyond All Reason installed locally, including its engine and game files.
- Network access between the host and joining computers.

Install the main dependencies with pacman:

```bash
sudo pacman -S --needed python python-pyqt6 iproute2
```

`iproute2` provides the `ip` command used for network-interface and LAN-address detection. The application can still be opened without a successful map scan, but setting up the correct BAR paths is required to launch the game.

Optional: the launcher's automatic firewall helper supports `ufw` or `firewalld` and uses `pkexec` to request permission. If you do not use either supported firewall manager, configure your firewall manually.

## Install and launch

1. Put `awesome_bar_lan_gui_arch.py`, `awesome_bar_lan_gui_arch.sh`, and this README in one folder.
2. Open a terminal in that folder.
3. Make the launcher executable and run it:

   ```bash
   chmod +x awesome_bar_lan_gui_arch.sh
   ./awesome_bar_lan_gui_arch.sh
   ```

The launcher checks for Python and PyQt6. If PyQt6 is missing and you run the launcher interactively, it offers to install `python-pyqt6` with pacman. You can also launch the application directly:

```bash
python3 awesome_bar_lan_gui_arch.py
```

Running it from a terminal is helpful when debugging launch or environment problems.

## First-run BAR configuration

Open **Settings** in the application and check the following paths:

### BAR data folder

The default Linux path is:

```text
~/.local/state/Beyond All Reason
```

If your BAR installation stores data elsewhere, use **Browse…** and choose the data folder. The application looks for engine directories beneath the data folder's `engine/` directory.

### Engine folder

Choose the engine-version directory that contains the executable named `spring`. For example, it may be inside a versioned subfolder under:

```text
~/.local/state/Beyond All Reason/engine/
```

The selected engine directory should contain `spring`; map/game option discovery also requires the engine's `libunitsync.so` in that directory. Click **Detect engine** to select the newest engine the application can find, or browse to the correct directory manually.

Use **Check setup** to verify the selected engine and data folder. Click **Save settings** to save the BAR paths.

### Map and game options

The program tries to load a cached map list when it starts. On first launch, or after BAR/maps update, open **Host a match → Match** and click **Refresh maps & options**. This launches a child process to query BAR's `libunitsync.so`. The first scan can take a while. If discovery fails, you can still type the map title exactly as BAR displays it; the game and map options may remain unavailable until the engine path is fixed.

## Light and Dark GUI themes

1. Open **Settings**.
2. Find **GUI appearance**.
3. Select **Light mode** or **Dark mode**.

The theme changes immediately. The preference is saved automatically and restored the next time the application starts. The setting is stored in `settings.json` (see **Where the application stores files** below).

## Host a match

### 1. Configure the match

Open **Host a match → Match** and choose or enter:

- Map name.
- Host port (default `8452`).
- Start position mode: Choose in game, Random, or Fixed.
- Start-box layout, including custom boxes if needed.
- Optional engine settings: minimum/maximum game speed, start countdown, fixed random seed, and map damage.

Start boxes are mainly relevant when start positions are set to **Choose in game**. The host's selected game/map options are also available in the corresponding tabs when BAR's option metadata has loaded.

### 2. Configure Players & teams

Open **Host a match → Players & teams**. The host can add friends, spectators, and bots; set player names; set teams, bonuses, sides, and colors; select AI types; and apply a team-layout preset.

- Maximum **8 human slots**, including the host and spectators.
- Maximum **16 total player/bot slots**.
- Players using the same team number are allies.
- Available factions in this build: **Armada, Cortex, Legion, Awesome**.
- Set one shared match password for players joining the lobby/game.
- Give each friend the exact player name assigned to their row. Names need to match the host's list.

The host may save and load a lobby preset using **Save lobby preset…** and **Load lobby preset…**.

### 3. Configure ports and invite friends

The lobby settings preview listens on **TCP** using the same port number configured for the BAR game, while BAR itself uses **UDP** for game connections.

- **Same LAN:** Use **Refresh LAN IP** to see the host's local address. Friends on the same network should enter that LAN IPv4 address, the same port, their assigned name, and the shared password.
- **Internet:** Use **Detect address** and follow the app's current network guidance. Usually, forward the chosen port on your router for **both UDP and TCP**, or use a compatible VPN port-forwarding service.
- **Copy invites:** Copies an invite block for each assigned human player/spectator. It contains the address, port, assigned name, and shared password; send it privately.
- **Allow port in firewall:** If `ufw` or `firewalld` is installed, the app attempts to add inbound rules for both protocols. Otherwise, add the rules manually.

For Proton VPN on Linux, the app looks for a WireGuard interface named `proton0` or `proton` followed by digits and attempts to request a forwarded port. The interface must exist and the VPN service must actually support and enable port forwarding. If the VPN uses another interface name or does not provide working port forwarding, configure the network manually or use a direct connection with router forwarding.

### 4. Open the lobby and start BAR

The host settings-preview server is started automatically by the application and remains available while the application runs. Keep the host application open while guests connect and while the match is being prepared.

When ready, click **START HOSTING**. The application applies queued guest changes, generates the host start script, and launches BAR. You do not need to wait for every assigned player to connect before starting; the host decides when to start. Guest lobby controls are locked after the host starts the match.

## Join a match

1. Open **Join a match**.
2. Enter the host's IPv4 address, port, the **exact assigned player name**, and the shared match password.
3. Click **Connect to lobby**.
4. Check the **Match** tab to view the host's current match configuration.
5. Open **Players & teams** to view the full roster.
6. If your assigned slot is a normal human player and the host has not started, use **Your settings** to edit only your own **Team, Bonus %, Side, and Color**.
7. Click **JOIN MATCH** when ready to launch BAR.

The roster, match details, and submitted settings update automatically while connected. The host is authoritative: guest edits are sent to the host and appear in the roster when applied. If your assigned slot is a spectator or bot, the guest editor remains disabled. Once the host starts, your settings become read-only.

**Note:** Connecting to the lobby preview and joining the actual BAR game are separate operations. The **JOIN MATCH** action attempts a lobby preview connection if necessary, but it still saves and launches the BAR client script if the preview is unavailable. A successful preview connection does not itself mean the BAR game connection will succeed.

## Player teams, bonus, faction, and color

- **Team:** Use the team number to choose which allies you play with. Players sharing the same team number are allies.
- **Bonus %:** Resource bonus/handicap. `0` is normal; the UI accepts values from `-100` to `1000`.
- **Side:** Armada, Cortex, Legion, or Awesome. The host script automatically adds BAR's `experimentallegionfaction` mod option when a non-spectator slot uses Legion.
- **Color:** Use the color picker to choose a team color. The program installs a local BAR Lua widget named `awesome_lan_team_colors.lua` under the selected BAR data directory's `LuaUI/Widgets/` folder so the chosen lobby colors can be reapplied in game. This widget must exist on each computer that needs to apply custom colors; the app attempts to install it when hosting or joining.

All clients should have matching BAR and map/game content. The application does not download maps, mods, units, or other game assets for guests.

## Generated game scripts and lobby presets

The application generates scripts for the BAR Spring engine. Buttons include **Save host script**, **Save join script**, and **Open generated scripts**.

By default, generated scripts and map-option cache files are stored under:

```text
~/.local/share/awesome-bar-lan/
├── GeneratedScripts/
└── MapCache/
```

The generated directory contains `awesome_host_script.txt` and `awesome_client_script.txt` when those scripts have been saved/launched. `MapCache/` holds discovered map names, map icons, and game/map option metadata.

Lobby presets are JSON files selected by the user through a file dialog. The application also saves its last lobby setup automatically when it closes.

## Where the application stores files

Unless the matching XDG environment variable is set, the application uses these paths:

| Purpose | Default path |
|---|---|
| Application data, generated scripts, and map cache | `~/.local/share/awesome-bar-lan/` |
| Settings, including appearance preference and BAR paths | `~/.config/awesome-bar-lan/settings.json` |
| Last lobby state | `~/.config/awesome-bar-lan/last_lobby.json` |
| BAR data directory default | `~/.local/state/Beyond All Reason/` |

If `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, or `XDG_STATE_HOME` is set, the corresponding directory is used instead. Do not delete these folders while the application is open.

**Password storage warning:** The last-lobby JSON and saved lobby presets can contain the shared password and roster information. Generated join scripts and copied invites also contain the password. Treat these files and invite messages as private, especially on shared computers.

## Network and security notes

- Keep the host application running while the lobby is in use. Closing it stops the preview server.
- The host preview requires inbound **TCP** on the configured port; actual BAR game traffic uses **UDP** on that port. Internet play commonly needs both protocols forwarded to the host machine.
- The lobby uses a password-based HMAC challenge-response rather than sending the password itself as the lobby authentication proof. However, the settings preview is **not TLS-encrypted**, so do not treat the connection as a secure tunnel. Use trusted networks and share the port/password only with people you intend to invite.
- The shared password is used for all invitees. The assigned player name selects which row a guest is allowed to edit; do not reuse ambiguous player names.
- Team/color/faction selections should be checked by the host before starting. Once the host clicks **START HOSTING**, further guest changes are locked for that session.
- Firewalls, router forwarding, VPN policy, CGNAT, and ISP restrictions can prevent connections even if the application is configured correctly.

## Troubleshooting

### The application will not start

Run it from a terminal to see the error:

```bash
python3 awesome_bar_lan_gui_arch.py
```

Then verify the dependencies:

```bash
python3 -c 'import PyQt6; print("PyQt6 import OK")'
```

Install missing dependencies with `sudo pacman -S --needed python python-pyqt6 iproute2`.

### The launcher says the Python file is missing

Check that the filenames and capitalization match exactly:

```text
awesome_bar_lan_gui_arch.sh
awesome_bar_lan_gui_arch.py
```

Keep both files in the same folder.

### No engine found, or map/game options cannot load

- Confirm **Settings → BAR data folder** points to the actual BAR data directory.
- Confirm **Engine folder** contains `spring` and `libunitsync.so`.
- Click **Detect engine**, or browse to the directory for the engine version used by BAR.
- Click **Save settings**, then **Refresh maps & options**.
- If scanning still fails, you can type the map name manually, but the match's options may not be discoverable until the engine setup is corrected.

### A friend cannot connect to the lobby preview

- Confirm the host app is still open and the port matches on both computers.
- Confirm the assigned name and shared password are correct.
- On the same LAN, use the host's LAN IPv4 address, not its public address.
- On the internet, use the host's reachable public/VPN address and forward/allow both TCP and UDP on the chosen port.
- If the host changed the port, make sure the updated port is configured and shared with guests.
- Check the host firewall and any router, VPN, CGNAT, or ISP restrictions.

### A guest can view the lobby but cannot edit settings

- Confirm the name entered by the guest exactly matches a **Player** row on the host. A host row, spectator, bot, or Lua AI row cannot be edited from the guest client.
- Confirm the host has not already clicked **START HOSTING**.
- Wait for a lobby refresh and check whether the host roster reflects the pending change.

### BAR starts but cannot connect or load the intended match

The lobby preview is separate from BAR's actual game connection. Verify the host's UDP reachability, engine/game/map versions, map title, and generated script settings. Check BAR's own logs for engine errors.

### The firewall button does not work

The automatic helper supports `ufw` and `firewalld` only. It may require a working `pkexec`/polkit authorization flow. If neither supported manager is present, add inbound rules for the configured UDP and TCP port with the firewall tools you use. Some `firewalld` rules added by the helper are runtime-only and may not survive a reload or reboot.

### Team colors do not appear in game

Check that the application can write to the selected BAR data directory and that this file exists:

```text
< BAR data folder >/LuaUI/Widgets/awesome_lan_team_colors.lua
```

Restart BAR after installing or updating the widget. All computers that need custom colors must have compatible game files and the local widget installed.

## Limitations

- Direct-connect only; there is no online matchmaking, relay, lobby chat, or automatic mod/map distribution.
- The tool configures and launches the BAR engine; it does not replace BAR itself.
- The application and networking behavior have to be tested on the actual host and client computers. A lobby-preview connection alone does not guarantee that BAR gameplay will connect successfully.
