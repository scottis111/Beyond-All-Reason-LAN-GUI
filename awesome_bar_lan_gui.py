#!/usr/bin/env python3
"""Awesome BAR LAN Lobby - Linux (Arch) port.

Simple GUI front end for direct-connect Beyond All Reason host/join scripts.
Needs: python, python-pyqt6   (sudo pacman -S python python-pyqt6)
"""
import array
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import urllib.request
import zlib
from pathlib import Path

APP_NAME = "awesome-bar-lan"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
APP_DIR = DATA_HOME / APP_NAME
GENERATED_DIR = APP_DIR / "GeneratedScripts"
MAPCACHE_DIR = APP_DIR / "MapCache"
SETTINGS_PATH = CONFIG_HOME / APP_NAME / "settings.json"

# Proton VPN's WireGuard tunnel interface on Linux is normally named proton0.
# If yours is named differently, change this pattern.
PROTON_IFACE_RE = re.compile(r"^proton\d*$", re.I)


class UserError(Exception):
    """An error whose message is meant to be shown to the user as-is."""


# ---------------------------------------------------------------------------
# Map scan worker (runs in a child process so a crashing/slow unitsync can't hurt the GUI)
# ---------------------------------------------------------------------------
def write_png(path, w, h, rgb):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    stride = w * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:(y + 1) * stride]) for y in range(h))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


def safe_icon_name(map_name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", map_name) + ".png"


def scan_main(engine_dir, data_dir, out_dir):
    import ctypes

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lib_path = Path(engine_dir) / "libunitsync.so"
    if not lib_path.is_file():
        print(f"ERROR libunitsync.so was not found in: {engine_dir}")
        return 2
    os.environ["SPRING_DATADIR"] = data_dir
    try:
        lib = ctypes.CDLL(str(lib_path))
    except OSError as e:
        print(f"ERROR could not load libunitsync.so: {e}")
        return 2
    lib.Init.argtypes = [ctypes.c_bool, ctypes.c_int]
    lib.Init.restype = ctypes.c_int
    lib.GetMapCount.restype = ctypes.c_int
    lib.GetMapName.argtypes = [ctypes.c_int]
    lib.GetMapName.restype = ctypes.c_char_p
    lib.GetMinimap.argtypes = [ctypes.c_char_p, ctypes.c_int]
    lib.GetMinimap.restype = ctypes.c_void_p
    lib.GetNextError.restype = ctypes.c_char_p
    if not lib.Init(False, 0):
        err = lib.GetNextError()
        print("ERROR unitsync Init failed: " + (err.decode("utf-8", "replace") if err else "unknown error"))
        return 2

    names = []
    for i in range(lib.GetMapCount()):
        raw = lib.GetMapName(i)
        if raw:
            name = raw.decode("utf-8", "replace")
            if name not in names:
                names.append(name)
    names.sort(key=str.lower)

    for old in out.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass

    mip, size = 3, 128  # 1024 >> 3
    for name in names:
        px = lib.GetMinimap(name.encode("utf-8"), mip)
        if not px:
            continue
        data = array.array("H")
        data.frombytes(ctypes.string_at(px, size * size * 2))
        rgb = bytearray(size * size * 3)
        j = 0
        for v in data:  # RGB565
            rgb[j] = ((v >> 11) & 31) * 255 // 31
            rgb[j + 1] = ((v >> 5) & 63) * 255 // 63
            rgb[j + 2] = (v & 31) * 255 // 31
            j += 3
        write_png(out / safe_icon_name(name), size, size, rgb)

    (out / "maps.json").write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    lib.UnInit()
    print(f"OK {len(names)}")
    return 0


if len(sys.argv) >= 5 and sys.argv[1] == "--scan":
    sys.exit(scan_main(sys.argv[2], sys.argv[3], sys.argv[4]))

# ---------------------------------------------------------------------------
# GUI (imported after the worker branch so the worker needs no Qt)
# ---------------------------------------------------------------------------
from PyQt6.QtCore import QProcess, QProcessEnvironment, QSize, Qt, QTimer, QUrl  # noqa: E402
from PyQt6.QtGui import QDesktopServices, QFont, QIcon, QPixmap  # noqa: E402
from PyQt6.QtWidgets import (QApplication, QComboBox, QFileDialog, QGridLayout, QGroupBox,  # noqa: E402
                             QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
                             QPushButton, QTabWidget, QVBoxLayout, QWidget)


# ---------------------------------------------------------------------------
# Networking helpers
# ---------------------------------------------------------------------------
def run_cmd(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def get_lan_ipv4():
    skip = re.compile(r"^(lo|proton|pvpn|ipv6leak|tun|tap|wg|docker|br-|veth|virbr|vmnet|tailscale|zt)", re.I)
    default_ifaces = set()
    for line in run_cmd(["ip", "-4", "route", "show", "default"]).splitlines():
        parts = line.split()
        if "dev" in parts:
            default_ifaces.add(parts[parts.index("dev") + 1])
    found = []
    for line in run_cmd(["ip", "-4", "-o", "addr", "show", "scope", "global"]).splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface, addr = parts[1], parts[3].split("/")[0]
        if skip.match(iface) or addr.startswith("169.254."):
            continue
        found.append((0 if iface in default_ifaces else 1, iface, addr))
    found.sort()
    return found[0][2] if found else ""


def get_public_ipv4():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "curl/8"}), timeout=6) as r:
                text = r.read().decode().strip()
            if isinstance(ipaddress.ip_address(text), ipaddress.IPv4Address):
                return text
        except Exception:
            continue
    return ""


def get_proton_iface():
    for line in run_cmd(["ip", "-o", "link", "show", "up"]).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name = parts[1].rstrip(":").split("@")[0]
            if PROTON_IFACE_RE.match(name):
                return name
    return None


def get_proton_gateways(iface):
    gws = []
    for line in run_cmd(["ip", "-4", "route", "show", "dev", iface]).splitlines():
        parts = line.split()
        if "via" in parts:
            gws.append(parts[parts.index("via") + 1])
    for d in ("10.2.0.1", "10.96.0.1"):
        if d not in gws:
            gws.append(d)
    return gws


def request_proton_port_forward(gateways):
    """NAT-PMP (RFC 6886) UDP mapping request, as used by Proton VPN port forwarding.
    Returns the assigned port or 0."""
    req = bytes([0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 60])
    for gw in gateways:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(2)
            s.sendto(req, (gw, 5351))
            data, _ = s.recvfrom(64)
            if len(data) >= 16 and data[1] == 129 and data[2] == 0 and data[3] == 0:
                port = data[10] * 256 + data[11]
                if port > 0:
                    return port
        except OSError:
            pass
        finally:
            s.close()
    return 0


def test_text_value(value, label):
    if not value.strip():
        raise UserError(f"{label} is required.")
    if re.search(r"[;\r\n{}]", value):
        raise UserError(f"{label} cannot contain semicolons, braces, or line breaks.")


def test_port(text):
    try:
        p = int(text)
    except ValueError:
        p = 0
    if p < 1 or p > 65535:
        raise UserError("Port must be a whole number from 1 to 65535.")
    return p


def guarded(fn):
    def wrapper(self, *_):
        try:
            return fn(self)
        except UserError as e:
            self.show_error(str(e))
        except Exception as e:  # noqa: BLE001
            self.show_error(f"{type(e).__name__}: {e}")
    return wrapper


HOST_TEMPLATE = """// Generated by Awesome BAR LAN GUI. MapName is the in-game map title, with no file extension.
[GAME]
{
    GameType=Beyond All Reason $VERSION;
    MapName=@@MAP@@;

    IsHost=1;
    HostIP=;
    HostPort=@@PORT@@;
    MyPlayerName=@@HOST@@;

    StartPosType=2;
    NumPlayers=2;
    NumTeams=2;
    NumAllyTeams=2;

    [PLAYER0]
    {
        Name=@@HOST@@;
        Team=0;
        Spectator=0;
    }

    [PLAYER1]
    {
        Name=@@FRIEND@@;
        Password=@@PASS@@;
        Team=1;
        Spectator=0;
    }

    [TEAM0]
    {
        TeamLeader=0;
        AllyTeam=0;
        Side=Awesome;
        RgbColor=0.9 0.6 0.2;
    }

    [TEAM1]
    {
        TeamLeader=1;
        AllyTeam=1;
        Side=Awesome;
        RgbColor=0.2 0.5 0.9;
    }

    [ALLYTEAM0]
    {
        NumAllies=0;
        StartRectLeft=0;
        StartRectTop=0;
        StartRectRight=1;
        StartRectBottom=0.2;
    }

    [ALLYTEAM1]
    {
        NumAllies=0;
        StartRectLeft=0;
        StartRectTop=0.8;
        StartRectRight=1;
        StartRectBottom=1;
    }
}
"""

CLIENT_TEMPLATE = """// Generated by Awesome BAR LAN GUI. The host IP is the host's public IPv4 for internet play (LAN IP only for same-network play).
[GAME]
{
    HostIP=@@IP@@;
    HostPort=@@PORT@@;
    SourcePort=0;
    MyPlayerName=@@NAME@@;
    MyPasswd=@@PASS@@;
    IsHost=0;
}
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Awesome BAR LAN Lobby")
        self.resize(900, 680)
        self.setMinimumSize(850, 650)
        self.map_pixmaps = {}
        self.pmp_gateways = []
        self.scan_proc = None
        self.scan_interactive = False
        self.pmp_timer = QTimer(self)
        self.pmp_timer.setInterval(45000)
        self.pmp_timer.timeout.connect(self.renew_proton_port)

        self.setStyleSheet("QMainWindow{background:#f2f5f9;} QTabWidget::pane{background:white;border:1px solid #c9d1db;}"
                           "QPushButton{padding:6px 12px;} QLineEdit,QComboBox{padding:4px;}")
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 8)
        outer.setSpacing(0)

        header = QWidget()
        header.setStyleSheet("background:#223043;")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(22, 10, 22, 12)
        title = QLabel("AWESOME BAR LAN LOBBY")
        title.setStyleSheet("color:white;")
        f = QFont()
        f.setPointSize(17)
        f.setBold(True)
        title.setFont(f)
        sub = QLabel("Simple direct-connect host and join launcher for your Awesome faction mod")
        sub.setStyleSheet("color:#dee7f1;")
        hl.addWidget(title)
        hl.addWidget(sub)
        outer.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 0)
        self.tabs = QTabWidget()
        body.addWidget(self.tabs, 1)
        outer.addLayout(body, 1)
        self.status = QLabel("Ready. Host and friend must have matching BAR engine, Awesome mod, and map files.")
        self.status.setStyleSheet("color:#37485b;")
        self.status.setWordWrap(True)
        self.status.setContentsMargins(20, 8, 20, 0)
        outer.addWidget(self.status)
        footer = QLabel("Direct LAN/IP launcher only — no online server, chat, matchmaking, or automatic mod distribution.")
        footer.setStyleSheet("color:#69717c;font-size:8pt;")
        footer.setContentsMargins(20, 2, 20, 0)
        outer.addWidget(footer)

        self.build_settings_tab()  # first: other tabs read these fields
        self.build_host_tab()
        self.build_join_tab()
        self.tabs.insertTab(0, self.tab_host, "Host a match")
        self.tabs.insertTab(1, self.tab_join, "Join a match")
        self.tabs.insertTab(2, self.tab_settings, "Settings")
        self.tabs.setCurrentIndex(0)
        QTimer.singleShot(250, self.initial_map_load)

    # ----- small UI helpers -----
    def set_status(self, msg):
        self.status.setText(msg)

    def show_message(self, msg, title="Awesome BAR LAN"):
        QMessageBox.information(self, title, msg)

    def show_error(self, msg):
        QMessageBox.critical(self, "Could not complete action", msg)

    @staticmethod
    def hint(text):
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setStyleSheet("color:#505a68;")
        return lb

    @staticmethod
    def button(text, handler, bold=False):
        b = QPushButton(text)
        b.clicked.connect(handler)
        if bold:
            f = b.font()
            f.setBold(True)
            b.setFont(f)
        return b

    # ----- settings tab -----
    def build_settings_tab(self):
        saved = {}
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        candidates = [STATE_HOME / "Beyond All Reason", Path.home() / ".local/state/Beyond All Reason"]
        default_data = next((c for c in candidates if c.is_dir()), candidates[0])
        self.tab_settings = QWidget()
        g = QGridLayout(self.tab_settings)
        g.setContentsMargins(25, 25, 25, 25)
        g.setVerticalSpacing(10)
        self.tb_data = QLineEdit(saved.get("DataDir") or str(default_data))
        self.tb_engine = QLineEdit(saved.get("EngineDir") or "")
        g.addWidget(QLabel("BAR data folder"), 0, 0, 1, 2)
        g.addWidget(self.tb_data, 1, 0)
        g.addWidget(self.button("Browse…", lambda: self.browse(self.tb_data, "Select the Beyond All Reason data folder")), 1, 1)
        g.addWidget(QLabel("Engine folder (contains the 'spring' executable)"), 2, 0, 1, 2)
        g.addWidget(self.tb_engine, 3, 0)
        g.addWidget(self.button("Browse…", lambda: self.browse(self.tb_engine, "Select the engine folder that contains spring")), 3, 1)
        g.addWidget(self.hint("Default data folder is ~/.local/state/Beyond All Reason. If BAR updates to another engine "
                              "version, click Detect engine or select the new engine folder here."), 4, 0, 1, 2)
        row = QHBoxLayout()
        row.addWidget(self.button("Save settings", self.on_save_settings))
        row.addWidget(self.button("Detect engine", self.on_detect_engine))
        row.addWidget(self.button("Open data folder", self.on_open_data))
        row.addWidget(self.button("Check setup", self.on_check_setup))
        row.addStretch(1)
        g.addLayout(row, 5, 0, 1, 2)
        g.setRowStretch(6, 1)
        g.setColumnStretch(0, 1)
        if not self.tb_engine.text() or not (Path(self.tb_engine.text()) / "spring").is_file():
            try:
                self.tb_engine.setText(str(self.find_engines()[0].parent))
            except Exception:
                pass

    def browse(self, field, caption):
        d = QFileDialog.getExistingDirectory(self, caption, field.text())
        if d:
            field.setText(d)

    def find_engines(self):
        root = Path(self.tb_data.text().strip()).expanduser() / "engine"
        found = []
        if root.is_dir():
            for d in root.iterdir():
                exe = d / "spring"
                if exe.is_file():
                    found.append(exe)
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return found

    def get_engine_exe(self):
        data = Path(self.tb_data.text().strip()).expanduser()
        if not data.is_dir():
            raise UserError(f"BAR data folder does not exist:\n{data}\nOpen Settings and choose the correct data folder.")
        eng = self.tb_engine.text().strip()
        if eng and (Path(eng) / "spring").is_file():
            return Path(eng) / "spring"
        engines = self.find_engines()
        if engines:
            self.tb_engine.setText(str(engines[0].parent))
            return engines[0]
        raise UserError(f"Could not find the 'spring' executable. Current engine folder:\n{eng}\n"
                        "Use Settings → Browse next to Engine folder.")

    @guarded
    def on_save_settings(self):
        data = self.tb_data.text().strip()
        if not data:
            raise UserError("Choose the BAR data folder first.")
        if not Path(data).expanduser().is_dir():
            raise UserError(f"BAR data folder does not exist:\n{data}")
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps({"DataDir": data, "EngineDir": self.tb_engine.text().strip()}, indent=2), encoding="utf-8")
        self.set_status("Settings saved.")
        self.show_message(f"Settings saved to:\n{SETTINGS_PATH}")

    @guarded
    def on_detect_engine(self):
        engines = self.find_engines()
        if not engines:
            raise UserError(f"No 'spring' executable was found under:\n{Path(self.tb_data.text().strip()).expanduser() / 'engine'}")
        self.tb_engine.setText(str(engines[0].parent))
        self.set_status(f"Detected engine: {engines[0].parent}")

    @guarded
    def on_open_data(self):
        p = Path(self.tb_data.text().strip()).expanduser()
        if not p.exists():
            raise UserError(f"Folder does not exist: {p}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    @guarded
    def on_check_setup(self):
        exe = self.get_engine_exe()
        data = Path(self.tb_data.text().strip()).expanduser()
        msg = f"Engine found:\n{exe}\n\nData folder:\n{data}"
        if (data / "games/BAR.sdd/modinfo.lua").exists():
            msg += "\n\nAwesome mod folder check: BAR.sdd found."
        else:
            msg += ("\n\nNOTE: games/BAR.sdd/modinfo.lua was not found at the default path. If your custom game uses "
                    "another folder name, this is only a warning.")
        if (data / "maps").exists():
            msg += "\nMaps folder exists."
        self.set_status("Setup check complete.")
        self.show_message(msg, "Setup check")

    # ----- host tab -----
    def build_host_tab(self):
        self.tab_host = QWidget()
        g = QGridLayout(self.tab_host)
        g.setContentsMargins(25, 22, 25, 18)
        g.setVerticalSpacing(10)
        self.tb_host_name = QLineEdit("gonj183notAlex")
        self.tb_friend_name = QLineEdit("FriendName")
        self.tb_host_pass = QLineEdit("awe123")
        self.tb_host_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.tb_host_port = QLineEdit("8452")
        self.tb_host_port.setMaximumWidth(120)

        self.tb_map = QComboBox()
        self.tb_map.setEditable(True)
        self.tb_map.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.tb_map.setIconSize(QSize(44, 44))
        self.tb_map.setMaxVisibleItems(9)
        self.tb_map.setEditText("All That Simmers v1.1.1")
        self.tb_map.editTextChanged.connect(self.update_map_preview)
        self.tb_map.currentTextChanged.connect(self.update_map_preview)
        self.pic_map = QLabel()
        self.pic_map.setFixedSize(76, 76)
        self.pic_map.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pic_map.setStyleSheet("border:1px solid #b9c2cd;background:#eef1f5;")
        map_row = QHBoxLayout()
        map_row.addWidget(self.tb_map, 1)
        map_row.addWidget(self.button("Refresh map list", self.on_refresh_maps))

        g.addWidget(QLabel("Your in-game player name"), 0, 0)
        g.addWidget(self.tb_host_name, 0, 1)
        g.addWidget(QLabel("Map (pick from list or type title)"), 1, 0)
        g.addLayout(map_row, 1, 1)
        g.addWidget(self.pic_map, 0, 2, 3, 1)
        g.addWidget(QLabel("Friend player name"), 2, 0)
        g.addWidget(self.tb_friend_name, 2, 1)
        g.addWidget(QLabel("Friend password"), 3, 0)
        g.addWidget(self.tb_host_pass, 3, 1)
        g.addWidget(QLabel("Host port"), 4, 0)
        g.addWidget(self.tb_host_port, 4, 1)
        g.addWidget(self.hint("Map title must match BAR. Both need the same mod and map. For internet play: with Proton VPN, "
                              "enable Port Forwarding and use a P2P server (the port is set automatically); without it, "
                              "forward the UDP port on your router."), 5, 0, 1, 3)

        box = QGroupBox("Address for far-away friends (Proton VPN if connected, otherwise public IP)")
        bg = QGridLayout(box)
        self.lbl_lan = QLabel("Your LAN IP: " + get_lan_ipv4())
        self.lbl_public = QLabel("Address: (click Detect)")
        for lb in (self.lbl_lan, self.lbl_public):
            f = lb.font()
            f.setBold(True)
            lb.setFont(f)
        bg.addWidget(self.lbl_lan, 0, 0)
        bg.addWidget(self.button("Refresh LAN IP", self.on_refresh_lan), 0, 1)
        bg.addWidget(self.lbl_public, 1, 0)
        bg.addWidget(self.button("Detect address", self.on_detect_address), 1, 1)
        bg.addWidget(self.button("Copy invite", self.on_copy_invite), 1, 2)
        bg.setColumnStretch(0, 1)
        g.addWidget(box, 6, 0, 1, 3)

        row = QHBoxLayout()
        row.addWidget(self.button("Save host script", self.on_save_host))
        row.addWidget(self.button("START HOSTING", self.on_start_host, bold=True))
        row.addWidget(self.button("Open generated scripts", self.on_open_generated))
        row.addWidget(self.button("Allow port in firewall", self.on_firewall))
        row.addStretch(1)
        g.addLayout(row, 7, 0, 1, 3)
        g.setRowStretch(8, 1)
        g.setColumnStretch(1, 1)

    # ----- join tab -----
    def build_join_tab(self):
        self.tab_join = QWidget()
        g = QGridLayout(self.tab_join)
        g.setContentsMargins(25, 25, 25, 18)
        g.setVerticalSpacing(10)
        self.tb_join_ip = QLineEdit()
        self.tb_join_port = QLineEdit("8452")
        self.tb_join_port.setMaximumWidth(120)
        self.tb_join_name = QLineEdit("FriendName")
        self.tb_join_pass = QLineEdit("awe123")
        self.tb_join_pass.setEchoMode(QLineEdit.EchoMode.Password)
        rows = [("Host IPv4 address (public IP for internet)", self.tb_join_ip), ("Host port", self.tb_join_port),
                ("Your in-game player name", self.tb_join_name), ("Match password", self.tb_join_pass)]
        for i, (label, w) in enumerate(rows):
            g.addWidget(QLabel(label), i, 0)
            g.addWidget(w, i, 1)
        g.addWidget(self.hint("Use the host's public IP (or LAN IP if on the same network), port, player name, and password "
                              "from their invite. Your player name and password must exactly match the values assigned for "
                              "you by the host."), 4, 0, 1, 2)
        row = QHBoxLayout()
        row.addWidget(self.button("Save join script", self.on_save_join))
        row.addWidget(self.button("JOIN MATCH", self.on_join, bold=True))
        row.addWidget(self.button("Show my LAN IP", self.on_refresh_lan))
        row.addStretch(1)
        g.addLayout(row, 5, 0, 1, 2)
        g.setRowStretch(6, 1)
        g.setColumnStretch(1, 1)

    # ----- script building / launching -----
    def build_host_script(self):
        host = self.tb_host_name.text().strip()
        map_name = self.tb_map.currentText().strip()
        friend = self.tb_friend_name.text().strip()
        password = self.tb_host_pass.text()
        port = test_port(self.tb_host_port.text().strip())
        test_text_value(host, "Host player name")
        test_text_value(map_name, "Map name")
        test_text_value(friend, "Friend player name")
        test_text_value(password, "Friend password")
        if re.search(r"\.(sd7|sdz|smf)$", map_name, re.I):
            raise UserError("Enter the map title as it appears in the BAR map list, without .sd7 or .smf. "
                            "Example: All That Simmers v1.1.1")
        return (HOST_TEMPLATE.replace("@@MAP@@", map_name).replace("@@PORT@@", str(port)).replace("@@HOST@@", host)
                .replace("@@FRIEND@@", friend).replace("@@PASS@@", password))

    def build_client_script(self):
        ip = self.tb_join_ip.text().strip()
        port = test_port(self.tb_join_port.text().strip())
        name = self.tb_join_name.text().strip()
        password = self.tb_join_pass.text()
        test_text_value(name, "Your in-game player name")
        test_text_value(password, "Password")
        if not ip:
            raise UserError("Enter the host computer’s IPv4 address (public IP for internet play).")
        try:
            ok = isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address)
        except ValueError:
            ok = False
        if not ok:
            raise UserError("Host IP must be an IPv4 address, for example 192.168.1.25.")
        return (CLIENT_TEMPLATE.replace("@@IP@@", ip).replace("@@PORT@@", str(port))
                .replace("@@NAME@@", name).replace("@@PASS@@", password))

    def save_script(self, text, filename, label):
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        path = GENERATED_DIR / filename
        path.write_text(text, encoding="utf-8")
        self.set_status(f"{label} saved: {path}")
        return path

    def launch_engine_script(self, script_path, activity):
        exe = self.get_engine_exe()
        data = str(Path(self.tb_data.text().strip()).expanduser())
        APP_DIR.mkdir(parents=True, exist_ok=True)
        log = open(APP_DIR / "last_engine.log", "wb")
        subprocess.Popen([str(exe), "--write-dir", data, str(script_path)], cwd=str(exe.parent),
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.set_status(f"{activity} started. If BAR opens but fails to connect, check script values and your firewall "
                        f"(engine log: {APP_DIR / 'last_engine.log'}).")

    # ----- connection endpoint (Proton VPN or direct) -----
    def resolve_host_endpoint(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            iface = get_proton_iface()
            if iface:
                gws = get_proton_gateways(iface)
                port = request_proton_port_forward(gws)
                if port <= 0:
                    raise UserError("Proton VPN is connected, but port forwarding is not active, so friends cannot reach "
                                    "you through it.\n\nIn the Proton VPN app: turn on Port Forwarding in Settings (paid "
                                    "plan), then connect to a P2P server and try again.\nOr disconnect Proton VPN to host "
                                    "using your own public IP.")
                ip = get_public_ipv4()
                if not ip:
                    raise UserError("Proton VPN is connected but the VPN exit IP could not be looked up.")
                self.pmp_gateways = gws
                self.tb_host_port.setText(str(port))
                self.pmp_timer.start()
                return {"mode": "proton", "ip": ip, "port": port}
            self.pmp_timer.stop()
            ip = get_public_ipv4()
            if not ip:
                raise UserError("Could not detect your public IP. Check your internet connection.")
            return {"mode": "direct", "ip": ip, "port": test_port(self.tb_host_port.text().strip())}
        finally:
            QApplication.restoreOverrideCursor()

    def renew_proton_port(self):
        if not get_proton_iface():
            self.pmp_timer.stop()
            return
        p = request_proton_port_forward(self.pmp_gateways)
        if p > 0 and str(p) != self.tb_host_port.text().strip():
            self.tb_host_port.setText(str(p))
            self.set_status(f"Proton forwarded port changed to {p}. Restart hosting and resend the invite.")

    # ----- host/join handlers -----
    @guarded
    def on_refresh_lan(self):
        ip = get_lan_ipv4()
        self.lbl_lan.setText(f"Your LAN IP: {ip}")
        self.set_status(f"Detected LAN address {ip}" if ip else
                        "No LAN IPv4 address found. Connect to Wi-Fi or Ethernet and refresh.")

    @guarded
    def on_detect_address(self):
        self.set_status("Detecting connection (Proton VPN or direct IP)...")
        ep = self.resolve_host_endpoint()
        if ep["mode"] == "proton":
            self.lbl_public.setText(f"Proton VPN: {ep['ip']} : {ep['port']}")
            self.set_status(f"Using Proton VPN port forwarding. Friends connect to {ep['ip']} port {ep['port']}.")
        else:
            self.lbl_public.setText(f"Public IP: {ep['ip']}")
            self.set_status(f"No Proton VPN detected. Using your public IP. Forward UDP {ep['port']} on your router.")

    @guarded
    def on_copy_invite(self):
        ep = self.resolve_host_endpoint()
        if ep["mode"] == "proton":
            self.lbl_public.setText(f"Proton VPN: {ep['ip']} : {ep['port']}")
        else:
            self.lbl_public.setText(f"Public IP: {ep['ip']}")
        invite = (f"Awesome BAR match\nHost IP: {ep['ip']}\nPort: {self.tb_host_port.text().strip()}\n"
                  f"Map: {self.tb_map.currentText().strip()}\nYour player name: {self.tb_friend_name.text().strip()}\n"
                  f"Password: {self.tb_host_pass.text()}")
        QApplication.clipboard().setText(invite)
        self.set_status("Invite copied. Send it privately. (Direct IP mode needs router port forwarding.)")

    @guarded
    def on_save_host(self):
        p = self.save_script(self.build_host_script(), "awesome_host_script.txt", "Host script")
        self.show_message(f"Host script saved to:\n{p}")

    @guarded
    def on_start_host(self):
        self.resolve_host_endpoint()
        p = self.save_script(self.build_host_script(), "awesome_host_script.txt", "Host script")
        self.launch_engine_script(p, "Host")

    @guarded
    def on_open_generated(self):
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(GENERATED_DIR)))

    @guarded
    def on_save_join(self):
        p = self.save_script(self.build_client_script(), "awesome_client_script.txt", "Join script")
        self.show_message(f"Join script saved to:\n{p}")

    @guarded
    def on_join(self):
        p = self.save_script(self.build_client_script(), "awesome_client_script.txt", "Join script")
        self.launch_engine_script(p, "Join")

    @guarded
    def on_firewall(self):
        port = test_port(self.tb_host_port.text().strip())
        if shutil.which("ufw"):
            cmd = ["pkexec", "ufw", "allow", f"{port}/udp"]
        elif shutil.which("firewall-cmd"):
            cmd = ["pkexec", "firewall-cmd", f"--add-port={port}/udp"]
        else:
            raise UserError(f"No ufw or firewalld was found. If you use nftables/iptables, allow inbound UDP port {port} "
                            "manually. If you have no firewall enabled, nothing needs to be done.")
        self.set_status(f"Requesting firewall rule for inbound UDP {port} (approve the authentication prompt)...")
        QApplication.processEvents()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise UserError(f"Firewall command failed:\n{' '.join(cmd)}\n\n{(r.stderr or r.stdout).strip()}")
        note = " (firewalld rule is runtime-only; it clears on reload/reboot)" if cmd[1] == "firewall-cmd" else ""
        self.set_status(f"Firewall rule added for inbound UDP {port}.{note}")

    # ----- map list + icons -----
    def update_map_preview(self, *_):
        text = self.tb_map.currentText()
        idx = self.tb_map.findText(text, Qt.MatchFlag.MatchExactly)
        if idx != self.tb_map.currentIndex():  # keep the icon inside the edit box in sync with the text
            self.tb_map.blockSignals(True)
            self.tb_map.setCurrentIndex(idx)
            self.tb_map.setEditText(text)
            self.tb_map.blockSignals(False)
        pm = self.map_pixmaps.get(text.strip())
        if pm:
            self.pic_map.setPixmap(pm.scaled(72, 72, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.pic_map.clear()

    def import_map_cache(self):
        list_path = MAPCACHE_DIR / "maps.json"
        if not list_path.is_file():
            return False
        try:
            names = json.loads(list_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        self.map_pixmaps = {}
        current = self.tb_map.currentText()
        self.tb_map.blockSignals(True)
        self.tb_map.clear()
        for n in names:
            f = MAPCACHE_DIR / safe_icon_name(n)
            pm = QPixmap(str(f)) if f.is_file() else QPixmap()
            if not pm.isNull():
                self.map_pixmaps[n] = pm
                self.tb_map.addItem(QIcon(pm), n)
            else:
                self.tb_map.addItem(n)
        self.tb_map.setEditText(current)
        self.tb_map.blockSignals(False)
        self.update_map_preview()
        return bool(names)

    def initial_map_load(self):
        if self.import_map_cache():
            self.set_status("Map list loaded from cache. Click Refresh map list after installing new maps.")
            return
        try:
            self.start_map_scan(interactive=False)
        except UserError as e:
            self.set_status(f"Map list not loaded: {e} You can still type the map title.")

    @guarded
    def on_refresh_maps(self):
        self.start_map_scan(interactive=True)

    def start_map_scan(self, interactive):
        if self.scan_proc is not None:
            return
        exe = self.get_engine_exe()
        engine_dir = str(exe.parent)
        data = str(Path(self.tb_data.text().strip()).expanduser())
        env = QProcessEnvironment.systemEnvironment()
        env.insert("SPRING_DATADIR", data)
        ld = env.value("LD_LIBRARY_PATH", "")
        env.insert("LD_LIBRARY_PATH", engine_dir + (":" + ld if ld else ""))
        proc = QProcess(self)
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.finished.connect(self.on_scan_finished)
        self.scan_proc = proc
        self.scan_interactive = interactive
        self.set_status("Scanning installed maps (the first scan can take a while)...")
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        proc.start(sys.executable, [os.path.abspath(__file__), "--scan", engine_dir, data, str(MAPCACHE_DIR)])

    def on_scan_finished(self, code, _status):
        QApplication.restoreOverrideCursor()
        proc, self.scan_proc = self.scan_proc, None
        out = bytes(proc.readAll()).decode("utf-8", "replace").strip()
        proc.deleteLater()
        last = out.splitlines()[-1] if out else ""
        if code == 0 and last.startswith("OK"):
            self.import_map_cache()
            self.set_status(f"Found {last.split()[-1]} installed maps.")
        else:
            msg = last[6:] if last.startswith("ERROR ") else (out[-400:] or f"scan exited with code {code}")
            self.set_status(f"Map list not loaded: {msg} You can still type the map title.")
            if self.scan_interactive:
                self.show_error(f"Map list not loaded:\n{msg}")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
