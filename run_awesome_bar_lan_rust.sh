#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
if [[ -x target/release/awesome_bar_lan_rust ]]; then exec target/release/awesome_bar_lan_rust; fi
if ! command -v cargo >/dev/null 2>&1; then
  echo "Rust/Cargo was not found and no compiled executable is present." >&2
  echo "Install Rust from https://rustup.rs/ or build this project on another computer." >&2
  exit 2
fi
cargo run --release
