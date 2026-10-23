# Awesome BAR LAN Lobby — Rust edition

## Build and run

Install the Rust stable toolchain (`rustup` and `cargo`), then run in this directory:

```sh
cargo run --release
```

Windows: double-click `Run_Awesome_BAR_LAN_Rust.bat`. Linux: run `./run_awesome_bar_lan_rust.sh`.

Build a distributable executable using `cargo build --release`. The output is `target/release/awesome_bar_lan_rust.exe` on Windows or `target/release/awesome_bar_lan_rust` on Linux. The first build downloads crates and may require platform GUI build dependencies.

## Included features

- BAR host script generation with `[GAME]`, `[PLAYERn]`, `[AIn]`, `[TEAMn]`, `[ALLYTEAMn]`, `[MODOPTIONS]` and `[MAPOPTIONS]` sections.
- Match map title, host port, start positions/start boxes, game speed/start options, map damage option, team layouts, team colors, up to 8 humans/spectators and 16 total player/bot slots.
- Armada, Cortex, Legion and Awesome faction choices, skirmish AI and Lua AI slot fields, and one shared password for human/spectator slots.
- Password-authenticated TCP lobby preview compatible with the Python program's challenge/HMAC-SHA256 protocol. A guest can change only their own Team, Bonus %, Side and Color; host-side validation rejects invalid edits and edits after hosting starts.
- Join-script creation, BAR engine detection/launch, engine logging, settings persistence, remembered lobby, JSON preset load/save, public and LAN IPv4 lookup, and the local team-color Lua widget.
- Reads existing `MapCache/maps.json` if present and allows manual map titles.

## Differences from the Python version

This is a core-workflow rewrite, not a line-for-line reproduction of every PyQt6 control. Automatic `unitsync` scanning of map thumbnails and game/map option definitions is not ported. Instead, game and map options are entered as `key=value`, one per line. Automatic Proton VPN NAT-PMP forwarding discovery is also not ported; enter the VPN forwarded port manually. Internet games generally require the host/router/firewall to pass both **UDP** (BAR game) and **TCP** (lobby preview) on the selected port. A compiled executable is not included because this environment has no Rust compiler installed.

## Files and data

Generated scripts and `last_engine.log` are stored under the per-user local data directory in `awesome-bar-lan/`. Settings and the last lobby preset are stored in the per-user configuration directory under `awesome-bar-lan/`.
