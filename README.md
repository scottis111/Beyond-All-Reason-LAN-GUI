AWESOME BAR LAN LOBBY - WINDOWS BUILD

This is a GUI lobby to interface with the BAR.sdd folder which has the modded Beyond All Reason content folder.

FILES
- awesome_bar_lan_gui_windows.py: Windows-adapted application
- Run_Awesome_BAR_LAN_GUI_Windows.bat: launcher; checks Python and offers to install PyQt6

SETUP
1. Keep both files in the same folder.
2. Install 64-bit Python 3 for Windows if it is not already installed.
3. Double-click Run_Awesome_BAR_LAN_GUI_Windows.bat.
4. The launcher will offer to install PyQt6 if it is missing.
5. In the app's Settings tab, verify the BAR data folder. For many Windows installs it is:
   %LOCALAPPDATA%\Programs\Beyond-All-Reason\data
6. Click Detect engine. The engine folder should contain spring.exe. Save settings.
7. Use Refresh maps & options to try to read installed map/game options from unitsync.dll.

PLAYER TABLE CHANGES
- Bot / AI is a dropdown. Choose "Bot (Skirmish AI)" or "Scavengers / Raptors (Lua AI)" in the Type column to enable it.
- If installed AI metadata is unavailable, the dropdown provides fallback BAR/Recoil AI entries.
- Side is a dropdown limited to Armada, Cortex, Legion, and Awesome.
- There is one Shared join password field above Players & teams. Set it once; all human players and spectators use that same password, with their own assigned player names.
- Copy invites supplies each friend their name and the same shared password.

WINDOWS NETWORK NOTES
- The LAN IP is detected using Windows networking APIs / PowerShell.
- The Allow port in firewall button requests administrator approval to add an inbound UDP rule.
- When using Proton VPN, turn on port forwarding in Proton and manually enter Proton's currently
  assigned forwarded port in the Host port field. This build does not auto-read Proton's Windows
  app port number. Without a VPN, forward the chosen UDP port on your router for internet hosting.

LIMITATIONS
The engine scan requires a compatible Windows unitsync.dll and its supporting DLLs in the selected engine folder. Use matching 64-bit
Python with the 64-bit BAR engine.
