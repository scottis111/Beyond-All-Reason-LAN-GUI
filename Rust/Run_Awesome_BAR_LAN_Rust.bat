@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0target\release\awesome_bar_lan_rust.exe" (
  start "Awesome BAR LAN Lobby" "%~dp0target\release\awesome_bar_lan_rust.exe"
  exit /b 0
)
where cargo >nul 2>&1
if errorlevel 1 (
  echo Rust/Cargo was not found and no compiled executable is present.
  echo Install the Rust toolchain from https://rustup.rs/ or build the project on another computer.
  pause
  exit /b 2
)
echo Building Awesome BAR LAN Lobby (Rust) for the first run...
cargo run --release
if errorlevel 1 (
  echo.
  echo Build or launch failed. Review the error above.
  pause
  exit /b 3
)
