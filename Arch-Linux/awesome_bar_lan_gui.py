#!/usr/bin/env python3
"""Awesome BAR LAN Lobby - Arch Linux build.

Direct-connect host/join launcher for Beyond All Reason with up to 8 human players,
bots (Skirmish AI and Lua AI such as Scavengers/Raptors), teams, start boxes, live
host-authoritative lobby synchronization, and light/dark GUI themes.

Arch Linux dependencies: python and python-pyqt6 (install with pacman).
"""
import array
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import socketserver
import threading
import urllib.request
import zlib
from pathlib import Path

APP_NAME = "awesome-bar-lan"
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    # Keep generated files/cache in LocalAppData and user settings in Roaming AppData.
    DATA_HOME = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    CONFIG_HOME = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    STATE_HOME = DATA_HOME
else:
    DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
APP_DIR = DATA_HOME / APP_NAME
GENERATED_DIR = APP_DIR / "GeneratedScripts"
MAPCACHE_DIR = APP_DIR / "MapCache"
SETTINGS_PATH = CONFIG_HOME / APP_NAME / "settings.json"
LAST_LOBBY_PATH = CONFIG_HOME / APP_NAME / "last_lobby.json"

MAX_HUMANS = 8      # host + friends + spectators
MAX_SLOTS = 16      # humans + bots (teams)

# Proton VPN's WireGuard tunnel interface on Linux is normally named proton0.
# If yours is named differently, change this pattern.
PROTON_IFACE_RE = re.compile(r"^proton\d*$", re.I)


class UserError(Exception):
    """An error whose message is meant to be shown to the user as-is."""


# ---------------------------------------------------------------------------
# unitsync scan worker (runs in a child process so a crashing/slow unitsync can't hurt the GUI)
# Writes into MapCache/: maps.json, <map>.png icons, mapoptions.json, gameinfo.json
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


OPT_TYPES = {1: "bool", 2: "list", 3: "number", 4: "string", 5: "section"}


def _s(b):
    return b.decode("utf-8", "replace") if b else ""


def _bind(lib, name, restype=None, argtypes=None):
    try:
        f = getattr(lib, name)
    except AttributeError:
        return None
    f.restype = restype
    if argtypes is not None:
        f.argtypes = argtypes
    return f


def _option_funcs(lib):
    import ctypes as C
    one = [C.c_int]
    two = [C.c_int, C.c_int]
    return {
        "key": _bind(lib, "GetOptionKey", C.c_char_p, one),
        "name": _bind(lib, "GetOptionName", C.c_char_p, one),
        "desc": _bind(lib, "GetOptionDesc", C.c_char_p, one),
        "section": _bind(lib, "GetOptionSection", C.c_char_p, one),
        "style": _bind(lib, "GetOptionStyle", C.c_char_p, one),
        "type": _bind(lib, "GetOptionType", C.c_int, one),
        "booldef": _bind(lib, "GetOptionBoolDef", C.c_int, one),
        "numdef": _bind(lib, "GetOptionNumberDef", C.c_float, one),
        "nummin": _bind(lib, "GetOptionNumberMin", C.c_float, one),
        "nummax": _bind(lib, "GetOptionNumberMax", C.c_float, one),
        "numstep": _bind(lib, "GetOptionNumberStep", C.c_float, one),
        "strdef": _bind(lib, "GetOptionStringDef", C.c_char_p, one),
        "strmax": _bind(lib, "GetOptionStringMaxLen", C.c_int, one),
        "listcount": _bind(lib, "GetOptionListCount", C.c_int, one),
        "listdef": _bind(lib, "GetOptionListDef", C.c_char_p, one),
        "itemkey": _bind(lib, "GetOptionListItemKey", C.c_char_p, two),
        "itemname": _bind(lib, "GetOptionListItemName", C.c_char_p, two),
        "itemdesc": _bind(lib, "GetOptionListItemDesc", C.c_char_p, two),
    }


def _read_options(F, count):
    def g(k, *a, d=""):
        f = F.get(k)
        return f(*a) if f else d

    out = []
    for i in range(count):
        try:
            t = OPT_TYPES.get(g("type", i, d=0), "unknown")
            o = {"key": _s(g("key", i, d=None)), "name": _s(g("name", i, d=None)), "desc": _s(g("desc", i, d=None)),
                 "section": _s(g("section", i, d=None)), "style": _s(g("style", i, d=None)), "type": t}
            if not o["name"]:
                o["name"] = o["key"]
            if t == "bool":
                o["def"] = 1 if g("booldef", i, d=0) else 0
            elif t == "number":
                o["def"] = float(g("numdef", i, d=0.0))
                o["min"] = float(g("nummin", i, d=0.0))
                o["max"] = float(g("nummax", i, d=1e6))
                o["step"] = float(g("numstep", i, d=1.0))
            elif t == "string":
                o["def"] = _s(g("strdef", i, d=None))
                o["maxlen"] = int(g("strmax", i, d=0))
            elif t == "list":
                o["def"] = _s(g("listdef", i, d=None))
                o["items"] = [{"key": _s(g("itemkey", i, j, d=None)), "name": _s(g("itemname", i, j, d=None)),
                               "desc": _s(g("itemdesc", i, j, d=None))} for j in range(int(g("listcount", i, d=0)))]
            if t != "unknown" and o["key"]:
                out.append(o)
        except Exception:  # noqa: BLE001 - one bad option must not lose the rest
            continue
    return out


def _scan_game(lib, F):
    import ctypes as C
    info = {"game": None, "sides": [], "lua_ais": [], "skirmish_ais": [], "mod_options": [], "error": None}
    try:
        mod_count = _bind(lib, "GetPrimaryModCount", C.c_int)()
        get_name = _bind(lib, "GetPrimaryModName", C.c_char_p, [C.c_int])
        get_arch = _bind(lib, "GetPrimaryModArchive", C.c_char_p, [C.c_int])
        mods = [(i, _s(get_name(i)), _s(get_arch(i))) for i in range(mod_count)]

        def is_bar(m):
            return "beyond all reason" in m[1].lower()

        cands = [m for m in mods if is_bar(m) and "dev" not in m[1].lower()] or [m for m in mods if is_bar(m)] or mods
        if not cands:
            raise RuntimeError("No game (mod) archives were found in the BAR data folder.")

        def verkey(m):
            nums = re.findall(r"\d+", m[1])
            return int(nums[0]) if nums else 0

        best = max(cands, key=verkey)
        info["game"] = {"name": best[1], "archive": best[2]}
        _bind(lib, "AddAllArchives", None, [C.c_char_p])(best[2].encode("utf-8"))

        side_count = _bind(lib, "GetSideCount", C.c_int)
        side_name = _bind(lib, "GetSideName", C.c_char_p, [C.c_int])
        if side_count and side_name:
            info["sides"] = [_s(side_name(i)) for i in range(side_count())]

        lua_count = _bind(lib, "GetLuaAICount", C.c_int)
        lua_name = _bind(lib, "GetLuaAIName", C.c_char_p, [C.c_int])
        lua_desc = _bind(lib, "GetLuaAIDesc", C.c_char_p, [C.c_int])
        if lua_count and lua_name:
            info["lua_ais"] = [{"name": _s(lua_name(i)), "desc": _s(lua_desc(i)) if lua_desc else ""}
                               for i in range(lua_count())]

        sk_count = _bind(lib, "GetSkirmishAICount", C.c_int)
        sk_info_count = _bind(lib, "GetSkirmishAIInfoCount", C.c_int, [C.c_int])
        info_key = _bind(lib, "GetInfoKey", C.c_char_p, [C.c_int])
        info_val = _bind(lib, "GetInfoValueString", C.c_char_p, [C.c_int])
        if sk_count and sk_info_count and info_key and info_val:
            for i in range(sk_count()):
                d = {}
                for j in range(sk_info_count(i)):
                    d[_s(info_key(j))] = _s(info_val(j))
                short = d.get("shortName") or d.get("name") or ""
                if not short or short.startswith(("Null", "Human")):
                    continue
                info["skirmish_ais"].append({"shortName": short, "name": d.get("name") or short,
                                             "version": d.get("version", ""), "description": d.get("description", "")})

        mod_opt_count = _bind(lib, "GetModOptionCount", C.c_int)
        if mod_opt_count:
            info["mod_options"] = _read_options(F, mod_opt_count())
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def scan_main(engine_dir, data_dir, out_dir):
    import ctypes

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    engine_path = Path(engine_dir)
    lib_name = "unitsync.dll" if IS_WINDOWS else "libunitsync.so"
    lib_path = engine_path / lib_name
    if not lib_path.is_file():
        print(f"ERROR {lib_name} was not found in: {engine_dir}")
        return 2
    os.environ["SPRING_DATADIR"] = data_dir
    dll_directory_handle = None
    if IS_WINDOWS:
        # The BAR engine ships DLL dependencies next to unitsync.dll. Windows does not
        # search the library's directory for dependencies in every Python setup unless
        # the directory is added explicitly.
        os.environ["PATH"] = str(engine_path) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                dll_directory_handle = os.add_dll_directory(str(engine_path))
            except OSError:
                dll_directory_handle = None
    try:
        lib = ctypes.CDLL(str(lib_path))
    except OSError as e:
        print(f"ERROR could not load {lib_name}: {e}")
        if IS_WINDOWS:
            print("Hint: use the matching 64-bit Python and keep the BAR engine DLL files together.")
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

    F = _option_funcs(lib)
    map_opt_count = _bind(lib, "GetMapOptionCount", ctypes.c_int, [ctypes.c_char_p])
    map_options = {}
    mip, size = 3, 128  # 1024 >> 3
    for name in names:
        px = lib.GetMinimap(name.encode("utf-8"), mip)
        if px:
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
        if map_opt_count:
            try:
                n = map_opt_count(name.encode("utf-8"))
                if n > 0:
                    opts = _read_options(F, n)
                    if opts:
                        map_options[name] = opts
            except Exception:  # noqa: BLE001
                pass

    # Game part last: AddAllArchives() narrows the virtual file system to the game.
    info = _scan_game(lib, F)

    (out / "maps.json").write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    (out / "mapoptions.json").write_text(json.dumps(map_options, ensure_ascii=False), encoding="utf-8")
    (out / "gameinfo.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    try:
        lib.UnInit()
    except Exception:  # noqa: BLE001
        pass
    if info.get("error"):
        print("WARN game options: " + info["error"])
    print(f"OK {len(names)}")
    return 0


if len(sys.argv) >= 5 and sys.argv[1] == "--scan":
    sys.exit(scan_main(sys.argv[2], sys.argv[3], sys.argv[4]))

# ---------------------------------------------------------------------------
# GUI (imported after the worker branch so the worker needs no Qt)
# ---------------------------------------------------------------------------
from PyQt6.QtCore import QProcess, QProcessEnvironment, QSize, Qt, QTimer, QUrl, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPalette, QPixmap  # noqa: E402
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox,  # noqa: E402
                             QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                             QHeaderView, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)


# ---------------------------------------------------------------------------
# Networking helpers
# ---------------------------------------------------------------------------
def run_cmd(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def get_lan_ipv4():
    # Windows has no built-in `ip` command with the Linux flags used below. Ask
    # PowerShell for an address on the best-metric physical interface first, then
    # fall back to Python's socket APIs if PowerShell is unavailable.
    if IS_WINDOWS:
        ps = (
            "$skip='(?i)(Proton|VPN|WireGuard|Wintun|TAP|TUN|Tailscale|ZeroTier|"
            "VirtualBox|VMware|Hyper-V|vEthernet|Docker|WSL)'; "
            "$c=Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address "
            "-and $_.InterfaceAlias -notmatch $skip } | "
            "Sort-Object { $_.NetAdapter.InterfaceMetric }; "
            "if($c){$c[0].IPv4Address[0].IPAddress}"
        )
        text = run_cmd(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], timeout=8).strip()
        for line in text.splitlines():
            try:
                addr = ipaddress.ip_address(line.strip())
                if isinstance(addr, ipaddress.IPv4Address) and not addr.is_loopback and not addr.is_link_local:
                    return str(addr)
            except ValueError:
                continue
        found = []
        try:
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
                addr = item[4][0]
                parsed = ipaddress.ip_address(addr)
                if isinstance(parsed, ipaddress.IPv4Address) and not parsed.is_loopback and not parsed.is_link_local:
                    found.append(str(parsed))
        except OSError:
            pass
        private = [a for a in found if ipaddress.ip_address(a).is_private]
        return (private or found or [""])[0]

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
    # The Proton adapter name and forwarded-port discovery below are Linux-specific.
    # On Windows, the user can enter the currently assigned Proton port in Host port.
    if IS_WINDOWS:
        return None
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


# ---------------------------------------------------------------------------
# Validation / formatting helpers
# ---------------------------------------------------------------------------
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


def fmt_num(v):
    v = float(v)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def guarded(fn):
    def wrapper(self, *_):
        try:
            return fn(self)
        except UserError as e:
            self.show_error(str(e))
        except Exception as e:  # noqa: BLE001
            self.show_error(f"{type(e).__name__}: {e}")
    return wrapper


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


PALETTE = [(0.9, 0.6, 0.2), (0.2, 0.5, 0.9), (0.3, 0.8, 0.3), (0.9, 0.2, 0.2), (0.7, 0.3, 0.9), (0.2, 0.8, 0.8),
           (0.95, 0.9, 0.2), (0.9, 0.5, 0.7), (0.6, 0.4, 0.2), (0.5, 0.5, 0.5), (0.1, 0.6, 0.4), (0.8, 0.8, 0.9),
           (0.4, 0.2, 0.6), (0.9, 0.3, 0.5), (0.3, 0.3, 0.8), (0.7, 0.9, 0.3)]

START_BOX_PRESETS = [("top_bottom", "Top vs bottom (2 teams)"), ("left_right", "Left vs right (2 teams)"),
                     ("corners", "Corners (up to 4 teams)"), ("strips", "Vertical strips (any number of teams)"),
                     ("full", "Whole map for everyone"), ("custom", "Custom (type the boxes yourself)")]
START_POS_TYPES = [(2, "Choose in game"), (1, "Random"), (0, "Fixed (map start positions)")]
TEAM_PRESETS = [("alternate", "Two teams (alternate in list order)"), ("ffa", "Free-for-all (everyone separate)"),
                ("humans_vs_bots", "All humans vs. all bots"), ("one_team", "Everyone on one team")]


def compute_start_rects(preset, n, custom_text):
    full = (0.0, 0.0, 1.0, 1.0)
    if n <= 0 or preset == "full":
        return [full] * max(n, 0)
    if preset == "custom":
        rects = []
        for line in custom_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                vals = [float(x) for x in re.split(r"[\s,;]+", line)]
            except ValueError:
                raise UserError(f"Custom start box line is not numeric: {line}")
            if len(vals) != 4 or any(v < 0 or v > 1 for v in vals):
                raise UserError("Each custom start box needs 4 numbers from 0 to 1:  left top right bottom")
            rects.append(tuple(vals))
        if len(rects) < n:
            raise UserError(f"Custom start boxes: you need {n} lines (one per team) but typed {len(rects)}.")
        return rects[:n]
    if preset == "top_bottom" and n == 2:
        return [(0.0, 0.0, 1.0, 0.2), (0.0, 0.8, 1.0, 1.0)]
    if preset == "left_right" and n == 2:
        return [(0.0, 0.0, 0.2, 1.0), (0.8, 0.0, 1.0, 1.0)]
    if preset == "corners" and n <= 4:
        return [(0.0, 0.0, 0.3, 0.3), (0.7, 0.7, 1.0, 1.0), (0.7, 0.0, 1.0, 0.3), (0.0, 0.7, 0.3, 1.0)][:n]
    return [(i / n, 0.0, (i + 1) / n, 1.0) for i in range(n)]  # strips (also the fallback)


LEGION_KEY = "experimentallegionfaction"
COLOR_MODOPT = "awesome_lan_colors"
COLOR_WIDGET_FILE = "awesome_lan_team_colors.lua"

COLOR_WIDGET_LUA = r"""-- Generated by Awesome BAR LAN GUI. Applies the team colors picked in the lobby.
function widget:GetInfo()
    return {
        name = "Awesome LAN Team Colors",
        desc = "Applies the team colors chosen in the Awesome BAR LAN lobby.",
        author = "Awesome BAR LAN GUI",
        date = "2026",
        license = "GNU GPL, v2 or later",
        layer = 100000,
        enabled = true,
    }
end

local wanted = {}
local active = false
local timer = 0

local function parse()
    wanted = {}
    local opts = Spring.GetModOptions() or {}
    local s = opts.awesome_lan_colors
    if type(s) ~= "string" then return false end
    for id, hex in s:gmatch("(%d+):(%x%x%x%x%x%x)") do
        wanted[tonumber(id)] = {
            tonumber(hex:sub(1, 2), 16) / 255,
            tonumber(hex:sub(3, 4), 16) / 255,
            tonumber(hex:sub(5, 6), 16) / 255,
        }
    end
    return next(wanted) ~= nil
end

local function apply()
    for teamID, c in pairs(wanted) do
        local r, g, b = Spring.GetTeamColor(teamID)
        if (not r) or math.abs(r - c[1]) > 0.004 or math.abs(g - c[2]) > 0.004 or math.abs(b - c[3]) > 0.004 then
            Spring.SetTeamColor(teamID, c[1], c[2], c[3])
        end
    end
end

function widget:Initialize()
    active = parse()
    if active then apply() end
end
function widget:GameStart()
    if active then apply() end
end
function widget:PlayerChanged()
    if active then apply() end
end
function widget:Update(dt)
    if not active then return end
    timer = timer + dt
    if timer > 3 then timer = 0; apply() end
end
"""


def color_modoption(teamslots, tid):
    """Encode teamID:rrggbb entries for the local color widget."""
    parts = []
    for slot in teamslots:
        r, g, b = (max(0, min(255, int(round(v * 255)))) for v in slot["color"])
        parts.append(f"{tid[id(slot)]}:{r:02x}{g:02x}{b:02x}")
    return ",".join(parts)


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


class LobbyPreviewRequestHandler(socketserver.StreamRequestHandler):
    """Password-protected lobby snapshot endpoint with per-player edit requests."""

    def handle(self):
        try:
            self.connection.settimeout(5.0)
            nonce = secrets.token_bytes(32)
            self.wfile.write(json.dumps({"challenge": nonce.hex()}).encode("utf-8") + b"\n")
            line = self.rfile.readline(16384)
            if not line or len(line) >= 16384:
                return
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                return

            cache = self.server.snapshot_provider()
            shared_password = str(cache.get("shared_password", ""))
            name = str(request.get("name", "")).strip()
            supplied = str(request.get("proof", ""))
            proof_message = nonce + b"\n" + name.lower().encode("utf-8")
            expected = hmac.new(shared_password.encode("utf-8"), proof_message, hashlib.sha256).hexdigest()
            if not shared_password or not name or not hmac.compare_digest(supplied, expected):
                response = {"ok": False, "error": "Password rejected, or the player name is missing. Use the host's assigned name and shared password."}
            else:
                settings = cache.get("settings", {})
                player = next((item for item in settings.get("players", [])
                               if str(item.get("name", "")).strip().lower() == name.lower()), None)
                edits = self.server.sanitize_edits(request.get("edits"))
                host_started = bool(cache.get("host_started", False))
                # Only a named human player can edit its own slot. Host, spectator, bot, and
                # unknown names remain read-only. The host GUI thread validates the target again.
                if edits and not host_started and player and player.get("kind") == "human":
                    self.server.pending_edits.put((name, edits))
                public_settings = dict(settings)
                public_settings["_host_started"] = host_started
                response = {"ok": True, "settings": public_settings}
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A malformed or disconnected client must not stop the lobby service.
            return


class LobbyPreviewTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, server_address, snapshot_provider):
        self.snapshot_provider = snapshot_provider
        self.pending_edits = queue.Queue()
        super().__init__(server_address, LobbyPreviewRequestHandler)

    @staticmethod
    def sanitize_edits(edits):
        """Whitelist and validate guest-editable values before queueing them."""
        if not isinstance(edits, dict):
            return {}
        clean = {}
        team = edits.get("team")
        if isinstance(team, int) and not isinstance(team, bool) and 1 <= team <= MAX_SLOTS:
            clean["team"] = team
        bonus = edits.get("bonus")
        if isinstance(bonus, int) and not isinstance(bonus, bool) and -100 <= bonus <= 1000:
            clean["bonus"] = bonus
        side = edits.get("side")
        if isinstance(side, str) and side in ("Armada", "Cortex", "Legion", "Awesome"):
            clean["side"] = side
        color = edits.get("color")
        if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            clean["color"] = color.lower()
        return clean


def request_lobby_preview(host_ip, port, password, player_name="", edits=None):
    """Fetch live host settings and optionally submit this player's own allowed edits."""
    try:
        name = str(player_name).strip()
        with socket.create_connection((host_ip, int(port)), timeout=4.0) as conn:
            conn.settimeout(5.0)
            reader = conn.makefile("rb")
            writer = conn.makefile("wb")
            first = reader.readline(8192)
            if not first:
                raise ValueError("The host closed the settings-preview connection.")
            challenge = json.loads(first.decode("utf-8"))
            nonce = bytes.fromhex(str(challenge.get("challenge", "")))
            if len(nonce) != 32:
                raise ValueError("The host returned an invalid authentication challenge.")
            proof_message = nonce + b"\n" + name.lower().encode("utf-8")
            proof = hmac.new(str(password).encode("utf-8"), proof_message, hashlib.sha256).hexdigest()
            writer.write(json.dumps({"proof": proof, "name": name, "edits": edits or {}}).encode("utf-8") + b"\n")
            writer.flush()
            response_line = reader.readline(4 * 1024 * 1024)
            if not response_line:
                raise ValueError("No settings response was received from the host.")
            response = json.loads(response_line.decode("utf-8"))
            if not response.get("ok"):
                raise PermissionError(response.get("error", "Host settings preview was rejected."))
            settings = response.get("settings")
            if not isinstance(settings, dict):
                raise ValueError("The host returned an invalid settings document.")
            return settings
    except PermissionError as exc:
        raise UserError(str(exc)) from exc
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise UserError(
            "Could not fetch the host lobby. Check the IP, port, assigned player name, shared password, "
            "and that TCP on this port is allowed/forwarded.\n\n" f"Details: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Dynamic options panel (game options / map options read from the installed game via unitsync)
# ---------------------------------------------------------------------------
class OptionsPanel(QWidget):
    def __init__(self, empty_text):
        super().__init__()
        self.opts = []
        self.controls = {}   # key -> (option dict, widget)
        self.rows = []       # (group box, label, widget, search text)
        self.pending = {}    # values to apply on the next set_options (from presets)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter options…")
        self.filter.textChanged.connect(self.apply_filter)
        self.chk_all = QCheckBox("Write every option, even defaults")
        self.chk_all.setToolTip("Normally only options you changed are written into the start script.")
        btn = QPushButton("Reset to defaults")
        btn.clicked.connect(self.reset)
        top.addWidget(self.filter, 1)
        top.addWidget(self.chk_all)
        top.addWidget(btn)
        v.addLayout(top)
        self.info = QLabel(empty_text)
        self.info.setWordWrap(True)
        self.info.setObjectName("optionsInfo")
        v.addWidget(self.info)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        v.addWidget(self.scroll, 1)

    # ----- building -----
    @staticmethod
    def default_string(o):
        t = o["type"]
        if t == "bool":
            return str(int(o.get("def", 0)))
        if t == "number":
            return fmt_num(o.get("def", 0))
        return str(o.get("def", ""))

    def make_control(self, o):
        t = o["type"]
        if t == "bool":
            w = QCheckBox()
            w.setChecked(bool(o.get("def", 0)))
        elif t == "list":
            w = QComboBox()
            for it in o.get("items", []):
                w.addItem(it.get("name") or it.get("key"), it.get("key"))
                if it.get("desc"):
                    w.setItemData(w.count() - 1, it["desc"], Qt.ItemDataRole.ToolTipRole)
            idx = w.findData(o.get("def", ""))
            w.setCurrentIndex(max(idx, 0))
            w.setMinimumWidth(240)
        elif t == "number":
            w = QDoubleSpinBox()
            vals = [o.get(k, 0.0) for k in ("def", "min", "max", "step")]
            w.setDecimals(0 if all(abs(x - round(x)) < 1e-9 for x in vals) else 3)
            lo, hi = o.get("min", 0.0), o.get("max", 1e6)
            if hi <= lo:
                hi = lo + 1e6
            w.setRange(lo, hi)
            w.setSingleStep(o.get("step") or 1.0)
            w.setValue(o.get("def", 0.0))
            w.setKeyboardTracking(False)
            w.setMinimumWidth(150)
            w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        else:
            w = QLineEdit(str(o.get("def", "")))
            if o.get("maxlen", 0) > 0:
                w.setMaxLength(o["maxlen"])
            w.setMinimumWidth(240)
        return w

    @staticmethod
    def widget_value(o, w):
        t = o["type"]
        if t == "bool":
            return "1" if w.isChecked() else "0"
        if t == "list":
            d = w.currentData()
            return "" if d is None else str(d)
        if t == "number":
            return fmt_num(w.value())
        return w.text()

    @staticmethod
    def set_widget(o, w, val):
        t = o["type"]
        try:
            if t == "bool":
                w.setChecked(str(val) in ("1", "true", "True"))
            elif t == "list":
                idx = w.findData(str(val))
                if idx >= 0:
                    w.setCurrentIndex(idx)
            elif t == "number":
                w.setValue(float(val))
            else:
                w.setText(str(val))
        except (ValueError, TypeError):
            pass

    def set_options(self, opts):
        prior = self.values(include_defaults=True) if self.controls else {}
        prior.update(self.pending)
        self.pending = {}
        self.opts = opts
        self.controls = {}
        self.rows = []
        inner = QWidget()
        vbox = QVBoxLayout(inner)
        sections = {o["key"]: o["name"] for o in opts if o["type"] == "section"}
        groups = {}
        for o in opts:
            if o["type"] in ("section", "unknown"):
                continue
            title = sections.get(o.get("section"), o.get("section") or "General")
            if title not in groups:
                box = QGroupBox(title)
                grid = QGridLayout(box)
                grid.setColumnStretch(0, 1)
                vbox.addWidget(box)
                groups[title] = [box, grid, 0]
            box, grid, r = groups[title]
            lbl = QLabel(o["name"])
            lbl.setWordWrap(True)
            w = self.make_control(o)
            if o.get("desc"):
                lbl.setToolTip(o["desc"])
                w.setToolTip(o["desc"])
            grid.addWidget(lbl, r, 0)
            grid.addWidget(w, r, 1)
            groups[title][2] = r + 1
            self.controls[o["key"]] = (o, w)
            self.rows.append((box, lbl, w, (o["name"] + " " + o["key"]).lower()))
        vbox.addStretch(1)
        self.scroll.setWidget(inner)
        for k, val in prior.items():
            if k in self.controls:
                o, w = self.controls[k]
                self.set_widget(o, w, val)
        self.info.setVisible(not self.controls)
        self.apply_filter()

    def set_empty_text(self, text):
        self.info.setText(text)
        self.info.setVisible(not self.controls)

    # ----- values -----
    def values(self, include_defaults=False):
        out = {}
        for k, (o, w) in self.controls.items():
            val = self.widget_value(o, w)
            default = self.default_string(o)
            if o["type"] == "number":
                differs = abs(float(val or 0) - float(default or 0)) > 1e-9
            else:
                differs = val != default
            if include_defaults or differs:
                out[k] = val
        return out

    def script_values(self):
        return self.values(include_defaults=self.chk_all.isChecked())

    def set_values(self, d):
        if not self.controls:
            self.pending = dict(d)
            return
        for k, val in d.items():
            if k in self.controls:
                o, w = self.controls[k]
                self.set_widget(o, w, val)

    def reset(self):
        for o, w in self.controls.values():
            self.set_widget(o, w, self.default_string(o))

    def apply_filter(self, *_):
        txt = self.filter.text().strip().lower()
        visible_boxes = set()
        for box, lbl, w, text in self.rows:
            show = (not txt) or (txt in text)
            lbl.setVisible(show)
            w.setVisible(show)
            if show:
                visible_boxes.add(id(box))
        for box, *_ in self.rows:
            box.setVisible(id(box) in visible_boxes)


# ---------------------------------------------------------------------------
# Players / bots / teams table
# ---------------------------------------------------------------------------
class PlayerTable(QTableWidget):
    # One host-controlled shared password lives above this table; passwords are not per-player.
    COLS = ["Type", "Name", "Team", "Bot / AI", "Bonus %", "Side", "Color"]
    FACTIONS = ["Armada", "Cortex", "Legion", "Awesome"]
    KINDS = [("Player", "human"), ("Spectator", "spec"), ("Bot (Skirmish AI)", "ai"),
             ("Scavengers / Raptors (Lua AI)", "luaai")]

    def __init__(self, win):
        super().__init__(0, len(self.COLS))
        self.win = win
        self.rows = []
        self.setHorizontalHeaderLabels(self.COLS)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for i, w in enumerate((165, 175, 65, 235, 80, 120, 55)):
            self.setColumnWidth(i, w)

    # ----- helpers -----
    @staticmethod
    def kind_of(r):
        return "host" if r["fixed"] else r["type"].currentData()

    def human_count(self):
        return sum(1 for r in self.rows if self.kind_of(r) in ("host", "human", "spec"))

    def unique_name(self, base, exclude=None):
        taken = {r["name"].text().strip().lower() for r in self.rows if r is not exclude}
        if base.lower() not in taken:
            return base
        n = 2
        while f"{base} {n}".lower() in taken:
            n += 1
        return f"{base} {n}"

    def paint_color(self, r):
        c = r["color"]
        r["colorbtn"].setStyleSheet(
            f"background-color: rgb({int(c[0] * 255)},{int(c[1] * 255)},{int(c[2] * 255)}); border:1px solid #555;")

    def pick_color(self, r):
        c = r["color"]
        picked = QColorDialog.getColor(QColor.fromRgbF(*c), self.win, "Team color")
        if picked.isValid():
            r["color"] = (picked.redF(), picked.greenF(), picked.blueF())
            self.paint_color(r)

    def fill_side_combo(self, r, keep=True):
        """Fill the faction list with the four factions requested for this BAR build."""
        cb = r["side"]
        cur = cb.currentText() if keep else ""
        cb.blockSignals(True)
        cb.clear()
        cb.addItems(self.FACTIONS)
        target = cur if cur in self.FACTIONS else self.win.default_side()
        idx = cb.findText(target)
        cb.setCurrentIndex(idx if idx >= 0 else 0)
        cb.blockSignals(False)

    def fill_ai_combo(self, r, keep=True):
        """Populate a real dropdown even if unitsync has not loaded its AI metadata yet."""
        cb = r["ai"]
        kind = self.kind_of(r)
        cur = cb.currentText().strip() if keep else ""
        cb.blockSignals(True)
        cb.clear()

        if kind == "ai":
            ais = self.win.game_info.get("skirmish_ais", [])
            for a in ais:
                short_name = a.get("shortName", "").strip()
                if not short_name:
                    continue
                display_name = a.get("name") or short_name
                version = a.get("version", "")
                label = f"{display_name} {version}".strip()
                cb.addItem(label, ("ai", short_name, version))
            if cb.count() == 0:
                # Common Recoil/BAR engine AI identifiers. They are fallbacks only;
                # a successful unitsync scan replaces them with the installed list.
                for short_name, label in (("BARb", "BARb (built-in)"),
                                          ("CircuitAI", "CircuitAI"),
                                          ("NullAI", "NullAI (no-op)")):
                    cb.addItem(label, ("ai", short_name, ""))

        elif kind == "luaai":
            lua_ais = self.win.game_info.get("lua_ais", [])
            for a in lua_ais:
                name = str(a.get("name", "")).strip()
                if name:
                    cb.addItem(name, ("luaai", name))
            if cb.count() == 0:
                # BAR Lua AI names commonly available in current game data.
                for name in ("SimpleAI", "SimpleDefenderAI", "SimpleConstructorAI", "ScavengersAI", "RaptorsAI"):
                    cb.addItem(name, ("luaai", name))

        # Preserve a saved selection where possible; otherwise select the first usable option.
        if cur:
            for i in range(cb.count()):
                data = cb.itemData(i)
                if cb.itemText(i) == cur or (isinstance(data, tuple) and len(data) > 1 and data[1] == cur):
                    cb.setCurrentIndex(i)
                    break
        if cb.count() and cb.currentIndex() < 0:
            cb.setCurrentIndex(0)
        cb.blockSignals(False)

    def ai_choice(self, r):
        cb = r["ai"]
        kind = self.kind_of(r)
        text = cb.currentText().strip()
        idx = cb.currentIndex()
        if idx >= 0 and cb.itemText(idx) == text:
            return cb.itemData(idx)
        if not text:
            return None
        return ("ai", text, "") if kind == "ai" else ("luaai", text)

    def apply_kind_state(self, r):
        kind = self.kind_of(r)
        is_human = kind in ("host", "human", "spec")
        is_bot = kind in ("ai", "luaai")
        r["ai"].setEnabled(is_bot)
        r["team"].setEnabled(kind != "spec")
        r["bonus"].setEnabled(kind != "spec")
        r["side"].setEnabled(kind != "spec")
        r["colorbtn"].setEnabled(kind != "spec")
        return is_human

    def on_kind_changed(self, r):
        self.apply_kind_state(r)
        kind = self.kind_of(r)
        if kind in ("ai", "luaai"):
            self.fill_ai_combo(r, keep=False)
            self.on_ai_changed(r)
        if kind in ("human", "spec") and r["auto_name"]:
            r["name"].setText(self.unique_name("Friend"))
        # All human players and spectators use the host's shared join password.

    def on_ai_changed(self, r):
        if self.kind_of(r) in ("ai", "luaai") and r["auto_name"]:
            base = r["ai"].currentText().strip().split(" ")[0] or "Bot"
            r["name"].blockSignals(True)
            r["name"].setText(self.unique_name(base, exclude=r))
            r["name"].blockSignals(False)

    # ----- rows -----
    def add_row(self, kind="human", name="", password="", ally=None, ai=None, bonus=0, side=None, color=None):
        if len(self.rows) >= MAX_SLOTS:
            raise UserError(f"At most {MAX_SLOTS} slots (players + bots) are supported.")
        if kind in ("host", "human", "spec") and self.human_count() >= MAX_HUMANS:
            raise UserError(f"At most {MAX_HUMANS} human players (including you and spectators) are supported.")
        r = {"fixed": kind == "host", "auto_name": not name}
        row = self.rowCount()
        self.insertRow(row)
        self.setRowHeight(row, 36)
        if r["fixed"]:
            r["type"] = QLabel("  Host (you)")
        else:
            cb = QComboBox()
            for label, key in self.KINDS:
                cb.addItem(label, key)
            cb.setCurrentIndex(max(0, [k for _, k in self.KINDS].index(kind)))
            cb.currentIndexChanged.connect(lambda _=0, rr=r: self.on_kind_changed(rr))
            r["type"] = cb
        r["name"] = QLineEdit(name)
        r["name"].textEdited.connect(lambda _=None, rr=r: rr.__setitem__("auto_name", False))
        r["team"] = QSpinBox()
        r["team"].setRange(1, MAX_SLOTS)
        nonspec = sum(1 for x in self.rows if self.kind_of(x) != "spec")
        r["team"].setValue(ally if ally else 1 + (nonspec % 2))
        r["ai"] = QComboBox()
        r["ai"].setEditable(False)
        r["ai"].setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        r["ai"].setMinimumContentsLength(12)
        r["ai"].setMaxVisibleItems(20)
        r["ai"].currentTextChanged.connect(lambda _=None, rr=r: self.on_ai_changed(rr))
        r["bonus"] = QSpinBox()
        r["bonus"].setRange(-100, 1000)
        r["bonus"].setValue(bonus)
        r["bonus"].setToolTip("Resource bonus/handicap in percent (0 = normal).")
        r["side"] = QComboBox()
        r["side"].setEditable(False)
        r["color"] = tuple(color) if color else PALETTE[len(self.rows) % len(PALETTE)]
        r["colorbtn"] = QPushButton()
        r["colorbtn"].clicked.connect(lambda _=False, rr=r: self.pick_color(rr))
        for col, key in enumerate(("type", "name", "team", "ai", "bonus", "side", "colorbtn")):
            self.setCellWidget(row, col, r[key])
        self.rows.append(r)
        self.paint_color(r)
        self.fill_side_combo(r, keep=False)
        if side:
            side_index = r["side"].findText(str(side))
            if side_index >= 0:
                r["side"].setCurrentIndex(side_index)
        self.apply_kind_state(r)
        if kind in ("ai", "luaai"):
            self.fill_ai_combo(r, keep=False)
            if ai:
                ai_text = str(ai).strip()
                ai_index = r["ai"].findText(ai_text)
                if ai_index < 0:
                    for i in range(r["ai"].count()):
                        data = r["ai"].itemData(i)
                        if isinstance(data, tuple) and len(data) > 1 and data[1] == ai_text:
                            ai_index = i
                            break
                if ai_index >= 0:
                    r["ai"].setCurrentIndex(ai_index)
            self.on_ai_changed(r)
        elif not name:
            r["name"].setText(self.unique_name("Friend" if kind == "human" else "Spectator"))
        return r

    def remove_selected(self):
        row = self.currentRow()
        if row < 0:
            raise UserError("Click a row first.")
        if self.rows[row]["fixed"]:
            raise UserError("The host row (you) cannot be removed.")
        self.removeRow(row)
        del self.rows[row]

    def clear_non_host(self):
        while len(self.rows) > 1:
            self.removeRow(len(self.rows) - 1)
            self.rows.pop()

    def refresh_lists(self):
        for r in self.rows:
            self.fill_side_combo(r)
            if self.kind_of(r) in ("ai", "luaai"):
                self.fill_ai_combo(r)

    def apply_team_preset(self, mode):
        i = 0
        for r in self.rows:
            kind = self.kind_of(r)
            if kind == "spec":
                continue
            if mode == "alternate":
                a = 1 + (i % 2)
            elif mode == "ffa":
                a = min(1 + i, MAX_SLOTS)
            elif mode == "humans_vs_bots":
                a = 2 if kind in ("ai", "luaai") else 1
            else:
                a = 1
            r["team"].setValue(a)
            i += 1

    def slots(self):
        out = []
        shared_password = self.win.tb_host_password.text()
        for r in self.rows:
            kind = self.kind_of(r)
            out.append({"kind": kind, "name": r["name"].text().strip(), "password": shared_password,
                        "ally": r["team"].value(), "ai": self.ai_choice(r) if kind in ("ai", "luaai") else None,
                        "ai_text": r["ai"].currentText().strip(), "bonus": r["bonus"].value(),
                        "side": r["side"].currentText().strip(), "color": r["color"]})
        return out


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    lobbyPollResult = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Awesome BAR LAN Lobby")
        self.resize(1100, 800)
        self.setMinimumSize(1000, 740)
        self.map_pixmaps = {}
        self.pmp_gateways = []
        self.scan_proc = None
        self.scan_interactive = False
        self.lobby_preview_server = None
        self.lobby_preview_thread = None
        self.lobby_preview_port = None
        self._lobby_share_cache = {"shared_password": "", "settings": {}, "host_started": False}
        self._host_started = False
        self._join_lobby_connected = False
        self._join_poll_busy = False
        self._join_creds = None
        self._join_pending_edits = {}
        self._join_last_settings = None
        self._join_color = (0.9, 0.6, 0.2)
        self.lobby_snapshot_timer = QTimer(self)
        self.lobby_snapshot_timer.setInterval(1000)
        self.lobby_snapshot_timer.timeout.connect(self.refresh_lobby_share_cache)
        self.lobby_server_restart_timer = QTimer(self)
        self.lobby_server_restart_timer.setSingleShot(True)
        self.lobby_server_restart_timer.setInterval(450)
        self.lobby_server_restart_timer.timeout.connect(self.ensure_lobby_preview_server)
        self.join_lobby_timer = QTimer(self)
        self.join_lobby_timer.setInterval(1500)
        self.join_lobby_timer.timeout.connect(self.poll_join_lobby)
        self.lobbyPollResult.connect(self.on_join_lobby_result)
        self.game_info = load_json(MAPCACHE_DIR / "gameinfo.json", {})
        self.map_options_all = load_json(MAPCACHE_DIR / "mapoptions.json", {})
        self._map_opts_key = None
        self.pmp_timer = QTimer(self)
        self.pmp_timer.setInterval(45000)
        self.pmp_timer.timeout.connect(self.renew_proton_port)

        # Theme styling is applied after all tabs/widgets have been constructed.
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 8)
        outer.setSpacing(0)

        header = QWidget()
        self.header_widget = header
        hl = QVBoxLayout(header)
        hl.setContentsMargins(22, 10, 22, 12)
        title = QLabel("AWESOME BAR LAN LOBBY")
        self.header_title = title
        f = QFont()
        f.setPointSize(17)
        f.setBold(True)
        title.setFont(f)
        sub = QLabel("Direct-connect host and join launcher — up to 8 players, bots, and all BAR game and map options")
        self.header_subtitle = sub
        hl.addWidget(title)
        hl.addWidget(sub)
        outer.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 0)
        self.tabs = QTabWidget()
        body.addWidget(self.tabs, 1)
        outer.addLayout(body, 1)
        self.status = QLabel("Ready. Everyone needs the same BAR engine, game version, and map files.")
        self.status.setObjectName("statusLabel")
        self.status.setWordWrap(True)
        self.status.setContentsMargins(20, 8, 20, 0)
        outer.addWidget(self.status)
        footer = QLabel("Direct-connect BAR launcher with password-protected host-settings preview; no matchmaking or automatic mod distribution.")
        self.footer = footer
        self.footer.setObjectName("footerLabel")
        footer.setContentsMargins(20, 2, 20, 0)
        outer.addWidget(footer)

        self.build_settings_tab()
        self.build_host_tab()
        self.build_join_tab()
        self.tabs.addTab(self.tab_host, "Host a match")
        self.tabs.addTab(self.tab_join, "Join a match")
        self.tabs.addTab(self.tab_settings, "Settings")
        self.cb_appearance.currentIndexChanged.connect(self.on_appearance_changed)
        self.apply_theme(self.cb_appearance.currentData() or "light")
        self.import_game_info()
        self.restore_last_lobby()
        self.refresh_lobby_share_cache()
        self.lobby_snapshot_timer.start()
        QTimer.singleShot(750, self.ensure_lobby_preview_server)
        QTimer.singleShot(250, self.initial_map_load)

    # ----- appearance / light and dark themes -----
    def theme_color(self, role):
        dark = getattr(self, "_appearance", "light") == "dark"
        colors = {
            "text": "#e5eaf0" if dark else "#263445",
            "muted": "#a8b3c2" if dark else "#596779",
            "warning": "#f3c969" if dark else "#8a6500",
            "error": "#ff8585" if dark else "#a33a3a",
            "success": "#8ce2a0" if dark else "#2b6b3a",
        }
        return colors.get(role, colors["text"])

    def apply_theme(self, theme):
        theme = str(theme or "light").lower()
        if theme not in ("light", "dark"):
            theme = "light"
        self._appearance = theme
        dark = theme == "dark"
        if dark:
            c = {
                "window": "#171c24", "base": "#202630", "panel": "#29313d",
                "border": "#414c5c", "text": "#e5eaf0", "muted": "#a8b3c2",
                "button": "#303947", "button_hover": "#3a4657", "highlight": "#356fa8",
                "highlighted_text": "#ffffff", "tooltip": "#303947", "tooltip_text": "#f3f4f6",
            }
        else:
            c = {
                "window": "#f2f5f9", "base": "#ffffff", "panel": "#e8edf4",
                "border": "#c6cfda", "text": "#263445", "muted": "#596779",
                "button": "#e8edf4", "button_hover": "#d8e1ec", "highlight": "#3973b9",
                "highlighted_text": "#ffffff", "tooltip": "#fffbe8", "tooltip_text": "#1f2937",
            }

        app = QApplication.instance()
        if app is not None:
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.Window, QColor(c["window"]))
            palette.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
            palette.setColor(QPalette.ColorRole.Base, QColor(c["base"]))
            palette.setColor(QPalette.ColorRole.AlternateBase, QColor(c["panel"]))
            palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["tooltip"]))
            palette.setColor(QPalette.ColorRole.ToolTipText, QColor(c["tooltip_text"]))
            palette.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
            palette.setColor(QPalette.ColorRole.Button, QColor(c["button"]))
            palette.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
            palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
            palette.setColor(QPalette.ColorRole.Highlight, QColor(c["highlight"]))
            palette.setColor(QPalette.ColorRole.HighlightedText, QColor(c["highlighted_text"]))
            palette.setColor(QPalette.ColorRole.Link, QColor(c["highlight"]))
            palette.setColor(QPalette.ColorRole.LinkVisited, QColor(c["highlight"]))
            palette.setColor(QPalette.ColorRole.Light, QColor(c["base"]))
            palette.setColor(QPalette.ColorRole.Mid, QColor(c["border"]))
            palette.setColor(QPalette.ColorRole.Dark, QColor(c["border"]))
            palette.setColor(QPalette.ColorRole.Shadow, QColor(c["window"]))
            try:
                palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["muted"]))
            except AttributeError:
                pass
            app.setPalette(palette)

        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {c['window']}; }}
            QWidget {{ color: {c['text']}; }}
            QTabWidget::pane {{ background-color: {c['window']}; border: 1px solid {c['border']}; }}
            QTabBar::tab {{ background-color: {c['panel']}; color: {c['text']};
                border: 1px solid {c['border']}; padding: 7px 12px; margin-right: 2px; }}
            QTabBar::tab:selected {{ background-color: {c['base']}; border-bottom-color: {c['base']}; }}
            QTabBar::tab:hover {{ background-color: {c['button_hover']}; }}
            QGroupBox {{ color: {c['text']}; border: 1px solid {c['border']};
                border-radius: 5px; margin-top: 11px; padding-top: 8px; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit,
            QTextEdit, QTableWidget {{ background-color: {c['base']}; color: {c['text']};
                border: 1px solid {c['border']}; border-radius: 3px;
                selection-background-color: {c['highlight']}; selection-color: {c['highlighted_text']}; padding: 3px; }}
            QComboBox QAbstractItemView {{ background-color: {c['base']}; color: {c['text']};
                selection-background-color: {c['highlight']}; selection-color: {c['highlighted_text']}; }}
            QHeaderView::section {{ background-color: {c['panel']}; color: {c['text']};
                border: 1px solid {c['border']}; padding: 4px; }}
            QTableWidget {{ gridline-color: {c['border']}; alternate-background-color: {c['panel']}; }}
            QPushButton {{ background-color: {c['button']}; color: {c['text']};
                border: 1px solid {c['border']}; border-radius: 4px; padding: 6px 12px; }}
            QPushButton:hover {{ background-color: {c['button_hover']}; }}
            QPushButton:pressed {{ background-color: {c['highlight']}; color: {c['highlighted_text']}; }}
            QPushButton:disabled {{ color: {c['muted']}; background-color: {c['panel']}; }}
            QCheckBox {{ color: {c['text']}; spacing: 6px; }}
            QToolTip {{ background-color: {c['tooltip']}; color: {c['tooltip_text']}; border: 1px solid {c['border']}; }}
            QScrollArea {{ background-color: {c['window']}; border: 1px solid {c['border']}; }}
            QLabel#awesomeHint, QLabel#optionsInfo {{ color: {c['muted']}; }}
            QLabel#statusLabel {{ color: {c['text']}; }}
            QLabel#footerLabel {{ color: {c['muted']}; font-size: 8pt; }}
            QLabel#mapPreview {{ border: 1px solid {c['border']}; background-color: {c['base']}; }}
        """)

        if hasattr(self, "header_widget"):
            self.header_widget.setStyleSheet(f"background-color: {'#141a23' if dark else '#223043'};")
            self.header_title.setStyleSheet("color:#ffffff;")
            self.header_subtitle.setStyleSheet(f"color:{'#d1dae6' if dark else '#dee7f1'};")
        if hasattr(self, "lbl_join_lobby_status"):
            self.lbl_join_lobby_status.setStyleSheet(f"color:{self.theme_color('muted')};")
        if hasattr(self, "pic_map"):
            self.pic_map.setStyleSheet(f"border:1px solid {c['border']}; background-color:{c['base']};")

    def on_appearance_changed(self, *_):
        theme = self.cb_appearance.currentData() or "light"
        self.apply_theme(theme)
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            saved = load_json(SETTINGS_PATH, {})
            if not isinstance(saved, dict):
                saved = {}
            saved["Appearance"] = theme
            SETTINGS_PATH.write_text(json.dumps(saved, indent=2), encoding="utf-8")
            self.set_status(f"{theme.capitalize()} mode selected. Appearance preference saved.")
        except OSError as exc:
            self.set_status(f"{theme.capitalize()} mode selected for this session, but its preference could not be saved: {exc}")

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
        lb.setObjectName("awesomeHint")
        lb.setWordWrap(True)
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

    def default_side(self):
        return "Awesome"

    # ----- settings tab -----
    def build_settings_tab(self):
        saved = load_json(SETTINGS_PATH, {})
        saved_appearance = str(saved.get("Appearance", "light")).lower()
        if saved_appearance not in ("light", "dark"):
            saved_appearance = "light"
        if IS_WINDOWS:
            local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
            candidates = [
                local / "Programs" / "Beyond-All-Reason" / "data",
                local / "Programs" / "Beyond All Reason" / "data",
                local / "Beyond-All-Reason" / "data",
                Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "Beyond All Reason" / "data",
            ]
        else:
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
        executable_name = "spring.exe" if IS_WINDOWS else "spring"
        g.addWidget(QLabel(f"Engine folder (contains {executable_name})"), 2, 0, 1, 2)
        g.addWidget(self.tb_engine, 3, 0)
        g.addWidget(self.button("Browse…", lambda: self.browse(self.tb_engine, f"Select the engine folder that contains {executable_name}")), 3, 1)
        if IS_WINDOWS:
            hint = (r"Windows default: %LOCALAPPDATA%\Programs\Beyond-All-Reason\data. "
                    "The BAR data folder should contain an engine subfolder. If BAR updates to another engine version, "
                    "click Detect engine or choose the new engine folder.")
        else:
            hint = ("Default data folder is ~/.local/state/Beyond All Reason. If BAR updates to another engine "
                    "version, click Detect engine or select the new engine folder here.")
        g.addWidget(self.hint(hint), 4, 0, 1, 2)
        row = QHBoxLayout()
        row.addWidget(self.button("Save settings", self.on_save_settings))
        row.addWidget(self.button("Detect engine", self.on_detect_engine))
        row.addWidget(self.button("Open data folder", self.on_open_data))
        row.addWidget(self.button("Check setup", self.on_check_setup))
        row.addStretch(1)
        g.addLayout(row, 5, 0, 1, 2)
        g.addWidget(QLabel("GUI appearance"), 6, 0)
        self.cb_appearance = QComboBox()
        self.cb_appearance.addItem("Light mode", "light")
        self.cb_appearance.addItem("Dark mode", "dark")
        self.cb_appearance.setCurrentIndex(max(0, self.cb_appearance.findData(saved_appearance)))
        self.cb_appearance.setToolTip("Choose the lobby GUI color scheme. Your choice is saved automatically.")
        g.addWidget(self.cb_appearance, 6, 1)
        g.addWidget(self.hint("Choose Light mode or Dark mode. The preference is saved automatically and restored next time."), 7, 0, 1, 2)
        g.setRowStretch(8, 1)
        g.setColumnStretch(0, 1)
        engine_name = "spring.exe" if IS_WINDOWS else "spring"
        if not self.tb_engine.text() or not (Path(self.tb_engine.text()) / engine_name).is_file():
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
        exe_name = "spring.exe" if IS_WINDOWS else "spring"
        found = []
        if root.is_dir():
            # Each BAR engine version normally has its own directory. Look one level down.
            for d in root.iterdir():
                exe = d / exe_name
                if exe.is_file():
                    found.append(exe)
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return found

    def get_engine_exe(self):
        data = Path(self.tb_data.text().strip()).expanduser()
        if not data.is_dir():
            raise UserError(f"BAR data folder does not exist:\n{data}\nOpen Settings and choose the correct data folder.")
        eng = self.tb_engine.text().strip()
        exe_name = "spring.exe" if IS_WINDOWS else "spring"
        if eng and (Path(eng) / exe_name).is_file():
            return Path(eng) / exe_name
        engines = self.find_engines()
        if engines:
            self.tb_engine.setText(str(engines[0].parent))
            return engines[0]
        raise UserError(f"Could not find the '{exe_name}' executable. Current engine folder:\n{eng}\n"
                        "Use Settings → Browse next to Engine folder.")

    @guarded
    def on_save_settings(self):
        data = self.tb_data.text().strip()
        if not data:
            raise UserError("Choose the BAR data folder first.")
        if not Path(data).expanduser().is_dir():
            raise UserError(f"BAR data folder does not exist:\n{data}")
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps({"DataDir": data, "EngineDir": self.tb_engine.text().strip(),
                                            "Appearance": self.cb_appearance.currentData() or "light"}, indent=2), encoding="utf-8")
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
        g = self.game_info.get("game")
        if g:
            msg += f"\n\nGame used for options: {g.get('name')}"
        if (data / "maps").exists():
            msg += "\nMaps folder exists."
        self.set_status("Setup check complete.")
        self.show_message(msg, "Setup check")

    # ----- host tab -----
    def build_host_tab(self):
        self.tab_host = QWidget()
        outer = QVBoxLayout(self.tab_host)
        outer.setContentsMargins(10, 10, 10, 10)
        self.host_tabs = QTabWidget()
        outer.addWidget(self.host_tabs, 1)

        # ---- Match page ----
        page = QWidget()
        g = QGridLayout(page)
        g.setContentsMargins(20, 16, 20, 12)
        g.setVerticalSpacing(8)
        self.tb_map = QComboBox()
        self.tb_map.setEditable(True)
        self.tb_map.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.tb_map.setIconSize(QSize(44, 44))
        self.tb_map.setMaxVisibleItems(9)
        self.tb_map.setEditText("All That Simmers v1.1.1")
        self.tb_map.editTextChanged.connect(self.update_map_preview)
        self.tb_map.currentTextChanged.connect(self.update_map_preview)
        self.pic_map = QLabel()
        self.pic_map.setFixedSize(84, 84)
        self.pic_map.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pic_map.setObjectName("mapPreview")
        map_row = QHBoxLayout()
        map_row.addWidget(self.tb_map, 1)
        map_row.addWidget(self.button("Refresh maps && options", self.on_refresh_maps))
        self.tb_host_port = QLineEdit("8452")
        self.tb_host_port.setMaximumWidth(120)
        self.tb_host_port.textChanged.connect(self.schedule_lobby_preview_restart)
        self.cb_startpos = QComboBox()
        for key, label in START_POS_TYPES:
            self.cb_startpos.addItem(label, key)
        self.cb_startbox = QComboBox()
        for key, label in START_BOX_PRESETS:
            self.cb_startbox.addItem(label, key)
        self.cb_startbox.currentIndexChanged.connect(self.on_startbox_changed)
        self.tb_custom_boxes = QPlainTextEdit()
        self.tb_custom_boxes.setPlaceholderText("One line per team, in team order:  left top right bottom   (numbers 0 to 1)\n"
                                                "Example:  0 0 1 0.25")
        self.tb_custom_boxes.setMaximumHeight(70)
        self.tb_custom_boxes.setVisible(False)

        g.addWidget(QLabel("Map (pick from list or type title)"), 0, 0)
        g.addLayout(map_row, 0, 1)
        g.addWidget(self.pic_map, 0, 2, 3, 1)
        g.addWidget(QLabel("Host port"), 1, 0)
        g.addWidget(self.tb_host_port, 1, 1)
        g.addWidget(QLabel("Start positions"), 2, 0)
        g.addWidget(self.cb_startpos, 2, 1)
        g.addWidget(QLabel("Start boxes"), 3, 0)
        g.addWidget(self.cb_startbox, 3, 1)
        g.addWidget(self.tb_custom_boxes, 4, 0, 1, 3)

        adv = QGroupBox("Engine options (leave blank for the engine default)")
        ag = QGridLayout(adv)
        self.tb_min_speed = QLineEdit()
        self.tb_max_speed = QLineEdit()
        self.tb_start_delay = QLineEdit()
        self.tb_seed = QLineEdit()
        for w, ph in ((self.tb_min_speed, "e.g. 1"), (self.tb_max_speed, "e.g. 3"), (self.tb_start_delay, "seconds"),
                      (self.tb_seed, "number")):
            w.setPlaceholderText(ph)
            w.setMaximumWidth(110)
        self.chk_no_damage = QCheckBox("Disable map damage (no terrain deformation)")
        ag.addWidget(QLabel("Min game speed"), 0, 0)
        ag.addWidget(self.tb_min_speed, 0, 1)
        ag.addWidget(QLabel("Max game speed"), 0, 2)
        ag.addWidget(self.tb_max_speed, 0, 3)
        ag.addWidget(QLabel("Start countdown"), 1, 0)
        ag.addWidget(self.tb_start_delay, 1, 1)
        ag.addWidget(QLabel("Fixed random seed"), 1, 2)
        ag.addWidget(self.tb_seed, 1, 3)
        ag.addWidget(self.chk_no_damage, 2, 0, 1, 4)
        ag.setColumnStretch(4, 1)
        g.addWidget(adv, 5, 0, 1, 3)

        address_title = ("Address for far-away friends (public/VPN IP)" if IS_WINDOWS else
                         "Address for far-away friends (Proton VPN if connected, otherwise public IP)")
        box = QGroupBox(address_title)
        bg = QGridLayout(box)
        self.lbl_lan = QLabel("Your LAN IP: " + get_lan_ipv4())
        self.lbl_public = QLabel("Address: (click Detect)")
        for lb in (self.lbl_lan, self.lbl_public):
            fnt = lb.font()
            fnt.setBold(True)
            lb.setFont(fnt)
        bg.addWidget(self.lbl_lan, 0, 0)
        bg.addWidget(self.button("Refresh LAN IP", self.on_refresh_lan), 0, 1)
        bg.addWidget(self.lbl_public, 1, 0)
        bg.addWidget(self.button("Detect address", self.on_detect_address), 1, 1)
        bg.addWidget(self.button("Copy invites", self.on_copy_invite), 1, 2)
        bg.setColumnStretch(0, 1)
        g.addWidget(box, 6, 0, 1, 3)
        if IS_WINDOWS:
            network_hint = ("For internet play on Windows: if using Proton VPN, enable Port Forwarding on a P2P server and "
                            "manually enter the active forwarded port shown in the Proton app into Host port. The game uses UDP; "
                            "the settings preview uses TCP on the same port number. Forward/allow both protocols when possible. ")
        else:
            network_hint = ("For internet play: with Proton VPN enable Port Forwarding and use a P2P server (the port is set "
                            "automatically); otherwise forward UDP for BAR and TCP for the settings preview using the same port number. ")
        g.addWidget(self.hint(network_hint + "Start boxes only matter for 'Choose in game'. If the team count doesn't fit "
                              "a 2-team box preset, vertical strips are used."), 7, 0, 1, 3)
        g.setRowStretch(8, 1)
        g.setColumnStretch(1, 1)
        self.host_tabs.addTab(page, "Match")

        # ---- Players page ----
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(14, 12, 14, 10)
        top = QHBoxLayout()
        self.cb_team_preset = QComboBox()
        for key, label in TEAM_PRESETS:
            self.cb_team_preset.addItem(label, key)
        top.addWidget(QLabel("Team layout"))
        top.addWidget(self.cb_team_preset)
        top.addWidget(self.button("Apply", self.on_apply_team_preset))
        top.addSpacing(18)
        top.addWidget(self.button("Add friend", self.on_add_friend))
        top.addWidget(self.button("Add spectator", self.on_add_spectator))
        top.addWidget(self.button("Add bot", self.on_add_bot))
        top.addWidget(self.button("Remove selected", self.on_remove_row))
        top.addSpacing(12)
        top.addWidget(QLabel("Shared join password:"))
        self.tb_host_password = QLineEdit()
        self.tb_host_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.tb_host_password.setPlaceholderText("Set one password for everyone")
        self.tb_host_password.setMaximumWidth(190)
        self.tb_host_password.setToolTip("Every human player and spectator uses this same password.")
        self.tb_host_password.textChanged.connect(lambda _=None: self.on_refresh_preview_if_ready())
        top.addWidget(self.tb_host_password)
        top.addStretch(1)
        v.addLayout(top)
        self.players = PlayerTable(self)
        v.addWidget(self.players, 1)
        host_row = self.players.add_row("host", name="gonj183notAlex", ally=1)
        self.tb_host_name = host_row["name"]
        self.players.add_row("human", name="FriendName", ally=2)
        v.addWidget(self.hint("Up to 8 humans in total (you + friends + spectators) and up to 16 players and bots. Teams with the same "
                              "number are allies. Choose a faction from Armada, Cortex, Legion, or Awesome. Bot / AI is a dropdown; "
                              "if installed AI metadata is not loaded, common BAR engine AI choices are shown. Set one shared join "
                              "password above; every friend or spectator uses that same password along with their assigned player name."))
        self.host_tabs.addTab(page, "Players && teams")

        # ---- Options pages ----
        unitsync_name = "unitsync.dll" if IS_WINDOWS else "libunitsync.so"
        self.game_panel = OptionsPanel("Game options are read from your installed BAR game. Click 'Refresh maps & options' "
                                       f"on the Match tab (needs the engine's {unitsync_name}) to load them.")
        self.map_panel = OptionsPanel("This map has no special map options.")
        self.host_tabs.addTab(self.game_panel, "Game options")
        self.host_tabs.addTab(self.map_panel, "Map options")

        # ---- Script preview ----
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(self.button("Refresh preview", self.on_refresh_preview))
        self.txt_preview = QPlainTextEdit()
        self.txt_preview.setReadOnly(True)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.txt_preview.setFont(mono)
        v.addWidget(self.txt_preview, 1)
        self.host_tabs.addTab(page, "Script preview")
        self.host_tabs.currentChanged.connect(lambda i: self.on_refresh_preview() if self.host_tabs.tabText(i) == "Script preview" else None)

        row = QHBoxLayout()
        row.addWidget(self.button("Save host script", self.on_save_host))
        row.addWidget(self.button("START HOSTING", self.on_start_host, bold=True))
        row.addWidget(self.button("Open generated scripts", self.on_open_generated))
        row.addWidget(self.button("Allow port in firewall", self.on_firewall))
        row.addSpacing(16)
        row.addWidget(self.button("Save lobby preset…", self.on_save_preset))
        row.addWidget(self.button("Load lobby preset…", self.on_load_preset))
        row.addStretch(1)
        outer.addLayout(row)

    def on_startbox_changed(self, *_):
        self.tb_custom_boxes.setVisible(self.cb_startbox.currentData() == "custom")

    @guarded
    def on_apply_team_preset(self):
        self.players.apply_team_preset(self.cb_team_preset.currentData())

    @guarded
    def on_add_friend(self):
        self.players.add_row("human")

    @guarded
    def on_add_spectator(self):
        self.players.add_row("spec")

    @guarded
    def on_add_bot(self):
        self.players.add_row("ai")

    @guarded
    def on_remove_row(self):
        self.players.remove_selected()

    def on_refresh_preview_if_ready(self):
        if hasattr(self, "txt_preview"):
            self.on_refresh_preview()

    @guarded
    def on_refresh_preview(self):
        try:
            self.txt_preview.setPlainText(self.build_host_script())
        except UserError as e:
            self.txt_preview.setPlainText("Cannot build the script yet:\n\n" + str(e))

    # ----- shared lobby settings preview service -----
    def apply_pending_lobby_edits(self):
        """Apply validated guest edits to the host's own player table on the GUI thread."""
        server = self.lobby_preview_server
        if server is None or self._host_started:
            return
        while True:
            try:
                player_name, edits = server.pending_edits.get_nowait()
            except queue.Empty:
                break
            target = None
            for row in self.players.rows:
                if self.players.kind_of(row) == "human" and row["name"].text().strip().lower() == player_name.strip().lower():
                    target = row
                    break
            if target is None:
                continue
            if "team" in edits:
                target["team"].setValue(int(edits["team"]))
            if "bonus" in edits:
                target["bonus"].setValue(int(edits["bonus"]))
            if "side" in edits:
                idx = target["side"].findText(edits["side"])
                if idx >= 0:
                    target["side"].setCurrentIndex(idx)
            if "color" in edits:
                h = edits["color"]
                target["color"] = (int(h[1:3], 16) / 255.0, int(h[3:5], 16) / 255.0, int(h[5:7], 16) / 255.0)
                self.players.paint_color(target)

    def refresh_lobby_share_cache(self):
        """Cache authoritative UI state and incorporate queued guest edits on the Qt thread."""
        try:
            self.apply_pending_lobby_edits()
            state = self.export_state()
            shared_password = str(state.get("shared_password", ""))
            state.pop("shared_password", None)
            # Never disclose either the shared password or per-row legacy password fields.
            for player in state.get("players", []):
                player.pop("password", None)
            self._lobby_share_cache = {"shared_password": shared_password, "settings": state,
                                       "host_started": self._host_started}
        except Exception:
            # Startup can briefly occur before every widget is ready.
            pass

    def get_lobby_share_cache(self):
        return self._lobby_share_cache

    def schedule_lobby_preview_restart(self, *_):
        if hasattr(self, "lobby_server_restart_timer"):
            self.lobby_server_restart_timer.start()

    def ensure_lobby_preview_server(self):
        """Listen for host settings on TCP at the same number as the game's UDP port."""
        try:
            port = int(self.tb_host_port.text().strip())
            if not 1 <= port <= 65535:
                return
        except (AttributeError, ValueError):
            return
        if self.lobby_preview_server is not None and self.lobby_preview_port == port:
            return
        self.stop_lobby_preview_server()
        try:
            server = LobbyPreviewTCPServer(("0.0.0.0", port), self.get_lobby_share_cache)
            thread = threading.Thread(target=server.serve_forever, name="AwesomeBARLobbyPreview", daemon=True)
            thread.start()
            self.lobby_preview_server = server
            self.lobby_preview_thread = thread
            self.lobby_preview_port = port
        except OSError as exc:
            self.lobby_preview_server = None
            self.lobby_preview_thread = None
            self.lobby_preview_port = None
            self.set_status(f"Host settings preview could not listen on TCP {port}: {exc}. BAR hosting can still work.")

    def stop_lobby_preview_server(self):
        server = self.lobby_preview_server
        self.lobby_preview_server = None
        self.lobby_preview_thread = None
        self.lobby_preview_port = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass

    @staticmethod
    def format_lobby_preview(settings):
        startpos_labels = {2: "Choose in game", 1: "Random", 0: "Fixed (map start positions)"}
        startbox_labels = {
            "top_bottom": "Top vs bottom", "left_right": "Left vs right", "corners": "Corners",
            "strips": "Vertical strips", "full": "Whole map for everyone", "custom": "Custom",
        }
        lines = [
            "HOST MATCH SETTINGS — read only",
            "=" * 42,
            f"Map: {settings.get('map') or '(not set)'}",
            f"Host port: {settings.get('port') or '(not set)'}",
            f"Start positions: {startpos_labels.get(settings.get('startpos'), settings.get('startpos', 'Default'))}",
            f"Start boxes: {startbox_labels.get(settings.get('startbox'), settings.get('startbox', 'Default'))}",
            f"Min game speed: {settings.get('min_speed') or 'engine default'}",
            f"Max game speed: {settings.get('max_speed') or 'engine default'}",
            f"Start countdown: {settings.get('start_delay') or 'engine default'}",
            f"Fixed random seed: {settings.get('seed') or 'engine default'}",
            f"Map damage: {'disabled' if settings.get('no_damage') else 'enabled'}",
        ]
        custom = str(settings.get("custom_boxes", "")).strip()
        if settings.get("startbox") == "custom" and custom:
            lines.extend(["Custom start boxes:", custom])

        lines.extend(["", "PLAYERS & TEAMS", "-" * 42])
        role_names = {"host": "Host", "human": "Player", "spec": "Spectator", "ai": "Bot (Skirmish AI)", "luaai": "Bot (Lua AI)"}
        players = settings.get("players") or []
        if not players:
            lines.append("No player slots were shared.")
        for player in players:
            kind = player.get("kind", "human")
            name = player.get("name") or "Unnamed"
            if kind == "spec":
                lines.append(f"{name} — Spectator")
                continue
            details = [role_names.get(kind, kind), f"Team {player.get('ally', 1)}"]
            side = player.get("side")
            if side:
                details.append(f"Faction: {side}")
            if kind in ("ai", "luaai") and player.get("ai"):
                details.append(f"AI: {player['ai']}")
            if player.get("bonus") not in (None, 0, "0"):
                details.append(f"Bonus: {player.get('bonus')}%")
            color = player.get("color")
            if isinstance(color, list) and len(color) == 3:
                details.append("Color RGB: " + ", ".join(f"{float(c):.2f}" for c in color))
            lines.append(f"{name} — " + ", ".join(details))

        for title, key in (("GAME OPTIONS", "game_options"), ("MAP OPTIONS", "map_options")):
            options = settings.get(key) or {}
            lines.extend(["", title, "-" * 42])
            if not options:
                lines.append("No options were shared (or option scanning has not loaded yet).")
            else:
                for option, value in options.items():
                    lines.append(f"{option} = {value}")
        lines.extend(["", "Password is not displayed in this preview.",
                      "This is the host's latest saved-in-memory selection, not a live BAR game-state feed."])
        return "\n".join(lines)

    # ----- join tab -----
    def build_join_tab(self):
        self.tab_join = QWidget()
        outer = QVBoxLayout(self.tab_join)
        outer.setContentsMargins(10, 10, 10, 10)

        connect_box = QWidget()
        g = QGridLayout(connect_box)
        g.setContentsMargins(15, 12, 15, 8)
        g.setVerticalSpacing(8)
        self.tb_join_ip = QLineEdit()
        self.tb_join_port = QLineEdit("8452")
        self.tb_join_port.setMaximumWidth(120)
        self.tb_join_name = QLineEdit("FriendName")
        self.tb_join_pass = QLineEdit()
        self.tb_join_pass.setEchoMode(QLineEdit.EchoMode.Password)
        rows = [("Host IPv4 address (public IP for internet)", self.tb_join_ip),
                ("Host port", self.tb_join_port), ("Your assigned in-game player name", self.tb_join_name),
                ("Shared match password", self.tb_join_pass)]
        for i, (label, w) in enumerate(rows):
            g.addWidget(QLabel(label), i, 0)
            g.addWidget(w, i, 1)
        g.addWidget(self.hint("Connect to the host's lobby to see the current Match settings and complete Players & teams roster. "
                              "You can edit only your own Team, Bonus %, Side, and Color. Everything else is read-only; "
                              "the host decides when to start."), 4, 0, 1, 2)
        row = QHBoxLayout()
        row.addWidget(self.button("Save join script", self.on_save_join))
        self.btn_join_lobby = self.button("Connect to lobby", self.on_toggle_lobby_connection)
        row.addWidget(self.btn_join_lobby)
        row.addWidget(self.button("JOIN MATCH", self.on_join, bold=True))
        row.addWidget(self.button("Show my LAN IP", self.on_refresh_lan))
        row.addStretch(1)
        g.addLayout(row, 5, 0, 1, 2)
        self.lbl_join_lobby_status = QLabel("Not connected to the host lobby.")
        self.lbl_join_lobby_status.setWordWrap(True)
        self.lbl_join_lobby_status.setStyleSheet(f"color:{self.theme_color('muted')};")
        g.addWidget(self.lbl_join_lobby_status, 6, 0, 1, 2)
        g.setColumnStretch(1, 1)
        outer.addWidget(connect_box)

        self.join_view_tabs = QTabWidget()
        # Match tab: comprehensive, read-only host setup.
        match_page = QWidget()
        mv = QVBoxLayout(match_page)
        mv.addWidget(self.hint("Live mirror of the host's Match tab. It is read-only and refreshes while connected."))
        self.txt_host_settings = QPlainTextEdit()
        self.txt_host_settings.setReadOnly(True)
        self.txt_host_settings.setPlaceholderText("Connect to the host lobby to load the Match tab.")
        self.txt_host_settings.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.txt_host_settings.setFont(font)
        mv.addWidget(self.txt_host_settings, 1)
        self.join_view_tabs.addTab(match_page, "Match")

        # Players & teams tab: immutable roster plus a separate editor for the local slot only.
        players_page = QWidget()
        pv = QVBoxLayout(players_page)
        pv.addWidget(self.hint("The roster is read-only. Use the four controls below to change only your own player slot. "
                               "Spectators, bots, and the host cannot be edited by guests."))
        self.join_players_table = QTableWidget(0, 7)
        self.join_players_table.setHorizontalHeaderLabels(["Type", "Name", "Team", "Bot / AI", "Bonus %", "Side", "Color"])
        self.join_players_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.join_players_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.join_players_table.verticalHeader().setVisible(False)
        self.join_players_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        pv.addWidget(self.join_players_table, 1)

        own = QGroupBox("Your settings")
        own_row = QHBoxLayout(own)
        self.join_team = QSpinBox()
        self.join_team.setRange(1, MAX_SLOTS)
        self.join_team.setToolTip("Players with the same team number are allies.")
        self.join_bonus = QSpinBox()
        self.join_bonus.setRange(-100, 1000)
        self.join_bonus.setToolTip("Resource bonus/handicap in percent; zero is normal.")
        self.join_side = QComboBox()
        self.join_side.setEditable(False)
        self.join_side.addItems(PlayerTable.FACTIONS)
        self.join_color_button = QPushButton("Choose color")
        self.join_team.valueChanged.connect(lambda value: self.note_join_edit("team", int(value)))
        self.join_bonus.valueChanged.connect(lambda value: self.note_join_edit("bonus", int(value)))
        self.join_side.currentTextChanged.connect(lambda value: self.note_join_edit("side", str(value)))
        self.join_color_button.clicked.connect(self.pick_join_color)
        for label, widget in (("Team", self.join_team), ("Bonus %", self.join_bonus),
                              ("Side", self.join_side), ("Color", self.join_color_button)):
            own_row.addWidget(QLabel(label))
            own_row.addWidget(widget)
        own_row.addStretch(1)
        pv.addWidget(own)
        self.join_editors_box = own
        self.set_join_editors_enabled(False)
        self.join_view_tabs.addTab(players_page, "Players && teams")
        outer.addWidget(self.join_view_tabs, 1)

    def set_join_editors_enabled(self, enabled):
        for widget in (getattr(self, "join_team", None), getattr(self, "join_bonus", None),
                       getattr(self, "join_side", None), getattr(self, "join_color_button", None)):
            if widget is not None:
                widget.setEnabled(bool(enabled))

    def join_identity(self):
        host_ip = self.tb_join_ip.text().strip()
        if not host_ip:
            raise UserError("Enter the host IP address first.")
        try:
            if not isinstance(ipaddress.ip_address(host_ip), ipaddress.IPv4Address):
                raise ValueError
        except ValueError as exc:
            raise UserError("Host IP must be an IPv4 address, for example 192.168.1.25.") from exc
        port = test_port(self.tb_join_port.text().strip())
        name = self.tb_join_name.text().strip()
        password = self.tb_join_pass.text()
        test_text_value(name, "Your in-game player name")
        test_text_value(password, "Shared match password")
        return host_ip, port, name, password

    def on_toggle_lobby_connection(self):
        if self._join_lobby_connected:
            self.join_lobby_timer.stop()
            self._join_lobby_connected = False
            self._join_creds = None
            self._join_pending_edits.clear()
            self.btn_join_lobby.setText("Connect to lobby")
            self.set_join_editors_enabled(False)
            self.lbl_join_lobby_status.setStyleSheet(f"color:{self.theme_color('muted')};")
            self.lbl_join_lobby_status.setText("Disconnected from the host lobby.")
            return
        self._join_creds = self.join_identity()
        self._join_lobby_connected = True
        self._join_poll_busy = False
        self._join_pending_edits.clear()
        self.btn_join_lobby.setText("Disconnect from lobby")
        self.lbl_join_lobby_status.setStyleSheet(f"color:{self.theme_color('muted')};")
        self.lbl_join_lobby_status.setText("Connecting to the host lobby…")
        self.tabs.setCurrentWidget(self.tab_join)
        self.join_lobby_timer.start()
        self.poll_join_lobby()

    def note_join_edit(self, key, value):
        if not self._join_lobby_connected:
            return
        if self._join_last_settings and self._join_last_settings.get("_host_started"):
            return
        if key not in ("team", "bonus", "side", "color"):
            return
        self._join_pending_edits[key] = value
        self.lbl_join_lobby_status.setStyleSheet(f"color:{self.theme_color('warning')};")
        self.lbl_join_lobby_status.setText("Your changes are being sent to the host…")

    def pick_join_color(self):
        picked = QColorDialog.getColor(QColor.fromRgbF(*self._join_color), self, "Choose your team color")
        if not picked.isValid():
            return
        self._join_color = (picked.redF(), picked.greenF(), picked.blueF())
        self.join_color_button.setStyleSheet(
            f"background-color: rgb({picked.red()},{picked.green()},{picked.blue()}); border:1px solid #555; padding:6px 12px;")
        self.note_join_edit("color", "#%02x%02x%02x" % (picked.red(), picked.green(), picked.blue()))

    def poll_join_lobby(self):
        if not self._join_lobby_connected or self._join_poll_busy or not self._join_creds:
            return
        self._join_poll_busy = True
        host_ip, port, name, password = self._join_creds
        edits = dict(self._join_pending_edits)

        def work():
            try:
                result = request_lobby_preview(host_ip, port, password, name, edits)
                self.lobbyPollResult.emit(result, None)
            except Exception as exc:
                self.lobbyPollResult.emit(None, str(exc))

        threading.Thread(target=work, name="AwesomeBARLobbyPoll", daemon=True).start()

    def on_join_lobby_result(self, settings, error):
        self._join_poll_busy = False
        if not self._join_lobby_connected:
            return
        if error:
            self.lbl_join_lobby_status.setStyleSheet(f"color:{self.theme_color('error')};")
            self.lbl_join_lobby_status.setText(str(error))
            self.set_join_editors_enabled(False)
            return

        self._join_last_settings = settings
        self.txt_host_settings.setPlainText(self.format_lobby_preview(settings))
        players = settings.get("players") or []
        my_name = self._join_creds[2].strip().lower() if self._join_creds else ""
        me = next((player for player in players
                   if str(player.get("name", "")).strip().lower() == my_name), None)
        host_started = bool(settings.get("_host_started", False))
        can_edit = bool(me and me.get("kind") == "human" and not host_started)
        self.render_join_players(settings, my_name)
        if me:
            # Only overwrite the editor values when no edit is pending or when the host snapshot
            # confirms that all pending fields have arrived.
            pending_applied = True
            for key, desired in self._join_pending_edits.items():
                current = self.join_player_field(me, key)
                if key == "color":
                    current = self.player_color_hex(me)
                    desired = str(desired).lower()
                if current != desired:
                    pending_applied = False
                    break
            if pending_applied:
                self._join_pending_edits.clear()
                self.load_join_player_editor(me)
        self.set_join_editors_enabled(can_edit)
        if host_started:
            status = "The host has started the match. Your lobby settings are now read-only."
        elif not me:
            status = "Connected, but your name is not in the host's player list. Ask the host to add it exactly."
        elif me.get("kind") != "human":
            status = "Connected. Your slot is a spectator or bot, so guest editing is disabled."
        elif self._join_pending_edits:
            status = "Connected. Waiting for the host to apply your changes…"
        else:
            status = "Connected. Lobby is live; you can edit Team, Bonus %, Side, and Color."
        self.lbl_join_lobby_status.setStyleSheet(f"color:{self.theme_color('success')};")
        self.lbl_join_lobby_status.setText(status + " Updated " + time.strftime("%H:%M:%S"))

    @staticmethod
    def player_color_hex(player):
        color = player.get("color")
        if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return color.lower()
        if isinstance(color, (list, tuple)) and len(color) == 3:
            return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(float(v) * 255)))) for v in color)
        return "#808080"

    @staticmethod
    def join_player_field(player, key):
        return {"team": int(player.get("ally", 1)), "bonus": int(player.get("bonus", 0)),
                "side": str(player.get("side", ""))}.get(key)

    def load_join_player_editor(self, player):
        for widget in (self.join_team, self.join_bonus, self.join_side):
            widget.blockSignals(True)
        self.join_team.setValue(max(1, min(MAX_SLOTS, int(player.get("ally", 1) or 1))))
        self.join_bonus.setValue(max(-100, min(1000, int(player.get("bonus", 0) or 0))))
        idx = self.join_side.findText(str(player.get("side", "Awesome")))
        self.join_side.setCurrentIndex(idx if idx >= 0 else self.join_side.findText("Awesome"))
        color = self.player_color_hex(player)
        self._join_color = (int(color[1:3], 16) / 255.0, int(color[3:5], 16) / 255.0,
                            int(color[5:7], 16) / 255.0)
        self.join_color_button.setStyleSheet(
            f"background-color: rgb({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)}); border:1px solid #555; padding:6px 12px;")
        for widget in (self.join_team, self.join_bonus, self.join_side):
            widget.blockSignals(False)

    def render_join_players(self, settings, my_name):
        players = settings.get("players") or []
        table = self.join_players_table
        table.setRowCount(len(players))
        role_names = {"host": "Host", "human": "Player", "spec": "Spectator", "ai": "Bot", "luaai": "Lua AI"}
        for row, player in enumerate(players):
            kind = player.get("kind", "human")
            mine = str(player.get("name", "")).strip().lower() == my_name
            spec = kind == "spec"
            values = [role_names.get(kind, str(kind)), str(player.get("name", "")) + (" (you)" if mine else ""),
                      "—" if spec else str(player.get("ally", "")), str(player.get("ai") or ""),
                      "—" if spec else str(player.get("bonus", 0)), "—" if spec else str(player.get("side", "")),
                      "" if spec else self.player_color_hex(player)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if mine:
                    font = item.font(); font.setBold(True); item.setFont(font)
                if col == 6 and not spec:
                    color = self.player_color_hex(player)
                    item.setBackground(QColor(color))
                    item.setForeground(QColor("white") if QColor(color).lightness() < 125 else QColor("black"))
                table.setItem(row, col, item)


    # ----- script building / launching -----
    def optional_number(self, widget, label, integer=False):
        text = widget.text().strip()
        if not text:
            return None
        try:
            v = float(text)
        except ValueError:
            raise UserError(f"{label} must be a number (or left blank).")
        if v < 0:
            raise UserError(f"{label} cannot be negative.")
        return str(int(v)) if integer else fmt_num(v)

    def build_host_script(self):
        host = self.tb_host_name.text().strip()
        map_name = self.tb_map.currentText().strip()
        port = test_port(self.tb_host_port.text().strip())
        test_text_value(host, "Host player name")
        test_text_value(map_name, "Map name")
        if re.search(r"\.(sd7|sdz|smf)$", map_name, re.I):
            raise UserError("Enter the map title as it appears in the BAR map list, without .sd7 or .smf. "
                            "Example: All That Simmers v1.1.1")
        slots = self.players.slots()
        humans = [s for s in slots if s["kind"] in ("host", "human", "spec")]
        teamslots = [s for s in slots if s["kind"] != "spec"]
        if len(humans) > MAX_HUMANS:
            raise UserError(f"At most {MAX_HUMANS} humans (you, friends and spectators) are supported.")
        if not teamslots:
            raise UserError("At least one non-spectator slot is needed.")
        if any(s["kind"] in ("human", "spec") for s in slots):
            test_text_value(self.tb_host_password.text(), "Shared join password")
        if len(teamslots) > MAX_SLOTS:
            raise UserError(f"At most {MAX_SLOTS} players + bots are supported.")
        seen = set()
        for s in slots:
            label = {"human": "Friend name", "spec": "Spectator name", "ai": "Bot name", "luaai": "Lua AI name",
                     "host": "Host player name"}[s["kind"]]
            test_text_value(s["name"], label)
            if s["name"].lower() in seen:
                raise UserError(f"Names must be unique. '{s['name']}' is used more than once.")
            seen.add(s["name"].lower())
            # All human/spectator PLAYER entries receive the single shared password below.
            if s["kind"] in ("ai", "luaai"):
                if not s["ai"] or not s["ai"][1]:
                    raise UserError(f"Choose a bot type for '{s['name']}'.")
                test_text_value(s["ai"][1], f"Bot type for {s['name']}")
                if s["kind"] == "ai" and s["ai"][2]:
                    test_text_value(s["ai"][2], f"Bot version for {s['name']}")
            if s["side"]:
                test_text_value(s["side"], f"Side for {s['name']}")

        used_allies = sorted({s["ally"] for s in teamslots})
        ally_index = {a: i for i, a in enumerate(used_allies)}
        rects = compute_start_rects(self.cb_startbox.currentData(), len(used_allies), self.tb_custom_boxes.toPlainText())

        pid = {}
        for i, s in enumerate(humans):
            pid[id(s)] = i
        tid = {}
        for i, s in enumerate(teamslots):
            tid[id(s)] = i

        L = []

        def kv(depth, key, val):
            L.append("    " * depth + f"{key}={val};")

        L.append("// Generated by Awesome BAR LAN GUI. MapName is the in-game map title, with no file extension.")
        L.append("[GAME]")
        L.append("{")
        kv(1, "GameType", "Beyond All Reason $VERSION")
        kv(1, "MapName", map_name)
        L.append("")
        kv(1, "IsHost", 1)
        kv(1, "HostIP", "")
        kv(1, "HostPort", port)
        kv(1, "MyPlayerName", host)
        L.append("")
        kv(1, "StartPosType", self.cb_startpos.currentData())
        kv(1, "NumPlayers", len(humans))
        kv(1, "NumTeams", len(teamslots))
        kv(1, "NumAllyTeams", len(used_allies))
        for key, widget, label, integer in (("MinSpeed", self.tb_min_speed, "Min game speed", False),
                                            ("MaxSpeed", self.tb_max_speed, "Max game speed", False),
                                            ("GameStartDelay", self.tb_start_delay, "Start countdown", True),
                                            ("FixedRNGSeed", self.tb_seed, "Fixed random seed", True)):
            val = self.optional_number(widget, label, integer)
            if val is not None:
                kv(1, key, val)
        if self.chk_no_damage.isChecked():
            kv(1, "DisableMapDamage", 1)

        for s in humans:
            i = pid[id(s)]
            L.append("")
            L.append(f"    [PLAYER{i}]")
            L.append("    {")
            kv(2, "Name", s["name"])
            if s["kind"] != "host":
                # Spring start scripts associate passwords with player names. Reuse one
                # host-controlled password for every non-host human/spectator slot.
                kv(2, "Password", self.tb_host_password.text())
            kv(2, "Team", tid.get(id(s), 0))
            kv(2, "Spectator", 1 if s["kind"] == "spec" else 0)
            L.append("    }")

        n_ai = 0
        for s in teamslots:
            if s["kind"] == "ai":
                L.append("")
                L.append(f"    [AI{n_ai}]")
                L.append("    {")
                kv(2, "Name", s["name"])
                kv(2, "ShortName", s["ai"][1])
                if s["ai"][2]:
                    kv(2, "Version", s["ai"][2])
                kv(2, "Team", tid[id(s)])
                kv(2, "Host", 0)
                L.append("    }")
                n_ai += 1

        for s in teamslots:
            L.append("")
            L.append(f"    [TEAM{tid[id(s)]}]")
            L.append("    {")
            kv(2, "TeamLeader", pid.get(id(s), 0))
            kv(2, "AllyTeam", ally_index[s["ally"]])
            if s["side"]:
                kv(2, "Side", s["side"])
            c = s["color"]
            kv(2, "RgbColor", f"{c[0]:.3f} {c[1]:.3f} {c[2]:.3f}")
            if s["bonus"]:
                kv(2, "Handicap", s["bonus"])
            if s["kind"] == "luaai":
                kv(2, "LuaAI", s["ai"][1])
            L.append("    }")

        for i, rc in enumerate(rects):
            L.append("")
            L.append(f"    [ALLYTEAM{i}]")
            L.append("    {")
            kv(2, "NumAllies", 0)
            kv(2, "StartRectLeft", fmt_num(round(rc[0], 4)))
            kv(2, "StartRectTop", fmt_num(round(rc[1], 4)))
            kv(2, "StartRectRight", fmt_num(round(rc[2], 4)))
            kv(2, "StartRectBottom", fmt_num(round(rc[3], 4)))
            L.append("    }")

        for section, panel in (("MODOPTIONS", self.game_panel), ("MAPOPTIONS", self.map_panel)):
            vals = dict(panel.script_values())
            if section == "MODOPTIONS":
                if any(s["side"].strip().lower() == "legion" for s in teamslots):
                    vals[LEGION_KEY] = "1"
                vals[COLOR_MODOPT] = color_modoption(teamslots, tid)
            if vals:
                L.append("")
                L.append(f"    [{section}]")
                L.append("    {")
                for k, val in vals.items():
                    if re.search(r"[;{}\r\n]", val) or re.search(r"[;{}\s=\r\n]", k):
                        raise UserError(f"Option '{k}' has a value with characters that cannot be written into a start script.")
                    kv(2, k, val)
                L.append("    }")
        L.append("}")
        return "\n".join(L) + "\n"

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
        log_path = APP_DIR / "last_engine.log"
        kwargs = {"cwd": str(exe.parent), "stdout": open(log_path, "wb"), "stderr": subprocess.STDOUT}
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen([str(exe), "--write-dir", data, str(script_path)], **kwargs)
        except Exception:
            kwargs["stdout"].close()
            raise
        self.set_status(f"{activity} started. If BAR opens but fails to connect, check script values and your firewall "
                        f"(engine log: {log_path}).")

    # ----- connection endpoint (Proton VPN or direct) -----
    def resolve_host_endpoint(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            if IS_WINDOWS:
                # Proton's Windows app displays its active forwarded port in its UI.
                # Do not run Linux `ip`/NAT-PMP discovery on Windows; use the user-entered
                # Host port, which should match Proton's active port when a VPN is in use.
                self.pmp_timer.stop()
                ip = get_public_ipv4()
                if not ip:
                    raise UserError("Could not detect your public/VPN IP. Check your internet connection.")
                return {"mode": "windows_manual", "ip": ip, "port": test_port(self.tb_host_port.text().strip())}
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
            self.set_status(f"Proton forwarded port changed to {p}. Restart hosting and resend the invites.")

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
        elif ep["mode"] == "windows_manual":
            self.lbl_public.setText(f"Public/VPN IP: {ep['ip']} : {ep['port']}")
            self.set_status("Windows mode: set Host port to the active Proton forwarded port when applicable; forward UDP for BAR "
                            "and TCP for the settings preview on the same port number.")
        else:
            self.lbl_public.setText(f"Public IP: {ep['ip']}")
            self.set_status(f"No Proton VPN detected. Using your public IP. Forward both UDP and TCP port {ep['port']} on your router.")

    @guarded
    def on_copy_invite(self):
        self.refresh_lobby_share_cache()
        self.ensure_lobby_preview_server()
        friends = [s for s in self.players.slots() if s["kind"] in ("human", "spec")]
        if not friends:
            raise UserError("Add at least one friend or spectator on the Players & teams tab first.")
        test_text_value(self.tb_host_password.text(), "Shared join password")
        ep = self.resolve_host_endpoint()
        if ep["mode"] == "proton":
            self.lbl_public.setText(f"Proton VPN: {ep['ip']} : {ep['port']}")
        elif ep["mode"] == "windows_manual":
            self.lbl_public.setText(f"Public/VPN IP: {ep['ip']} : {ep['port']}")
        else:
            self.lbl_public.setText(f"Public IP: {ep['ip']}")
        blocks = []
        for s in friends:
            role = "  (spectator)" if s["kind"] == "spec" else ""
            blocks.append(f"Awesome BAR match\nHost IP: {ep['ip']}\nPort: {self.tb_host_port.text().strip()}\n"
                          f"Map: {self.tb_map.currentText().strip()}\nYour player name: {s['name']}{role}\n"
                          f"Host settings preview: {ep['ip']} TCP port {self.tb_host_port.text().strip()}\n"
                          f"Shared password: {self.tb_host_password.text()}")
        QApplication.clipboard().setText("\n\n------------------------------\n\n".join(blocks))
        if IS_WINDOWS:
            note = ("If using Proton VPN, the Host port must match the active forwarded port shown in Proton; "
                    "otherwise forward both UDP (game) and TCP (settings preview) on that port on your router.")
        elif ep["mode"] == "proton":
            note = "Proton VPN port forwarding is active; share the displayed IP and port."
        else:
            note = "Direct IP mode needs router UDP forwarding for BAR and TCP forwarding for the settings preview (same port number)."
        self.set_status(f"Invites for {len(friends)} friend(s) copied, one block each. Send each block privately. {note}")

    @guarded
    def on_save_host(self):
        p = self.save_script(self.build_host_script(), "awesome_host_script.txt", "Host script")
        self.show_message(f"Host script saved to:\n{p}")

    def install_color_widget(self):
        """Install the local BAR widget that reapplies lobby-selected team colors."""
        data = Path(self.tb_data.text().strip()).expanduser()
        if not data.is_dir():
            return "Team colors could not be installed because the BAR data folder was not found."
        target = data / "LuaUI" / "Widgets" / COLOR_WIDGET_FILE
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_text(encoding="utf-8") != COLOR_WIDGET_LUA:
                target.write_text(COLOR_WIDGET_LUA, encoding="utf-8")
        except OSError as exc:
            return f"Team colors could not be installed ({exc})."
        return None

    @guarded
    def on_start_host(self):
        # Commit any guest edits already received before writing the final start script.
        self.apply_pending_lobby_edits()
        self.resolve_host_endpoint()
        self.refresh_lobby_share_cache()
        self.ensure_lobby_preview_server()
        p = self.save_script(self.build_host_script(), "awesome_host_script.txt", "Host script")
        self._host_started = True
        self.refresh_lobby_share_cache()
        color_warning = self.install_color_widget()
        self.launch_engine_script(p, "Host")
        if color_warning:
            self.set_status(self.status.text() + " Note: " + color_warning)

    @guarded
    def on_open_generated(self):
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(GENERATED_DIR)))

    @guarded
    def on_fetch_host_settings(self):
        # Kept as a compatibility handler for older saved UI layouts.
        self.on_toggle_lobby_connection()

    @guarded
    def on_save_join(self):
        p = self.save_script(self.build_client_script(), "awesome_client_script.txt", "Join script")
        self.show_message(f"Join script saved to:\n{p}")

    @guarded
    def on_join(self):
        # Connecting to BAR and connecting to the lobby viewer are separate operations.
        # Make a best-effort lobby connection, but never block a valid BAR join on preview failure.
        if not self._join_lobby_connected:
            try:
                self._join_creds = self.join_identity()
                self._join_lobby_connected = True
                self.btn_join_lobby.setText("Disconnect from lobby")
                self.join_lobby_timer.start()
                self.poll_join_lobby()
            except UserError as exc:
                self.txt_host_settings.setPlainText("LOBBY PREVIEW UNAVAILABLE\n\n" + str(exc))
        p = self.save_script(self.build_client_script(), "awesome_client_script.txt", "Join script")
        color_warning = self.install_color_widget()
        self.launch_engine_script(p, "Join")
        if color_warning:
            self.set_status(self.status.text() + " Note: " + color_warning)

    @guarded
    def on_firewall(self):
        port = test_port(self.tb_host_port.text().strip())
        self.set_status(f"Requesting firewall rules for inbound UDP and TCP {port}...")
        QApplication.processEvents()
        if IS_WINDOWS:
            # One UAC prompt adds both rules: UDP for BAR, TCP for the settings preview.
            elevated_script = (
                f"& netsh.exe advfirewall firewall add rule name=Awesome_BAR_UDP_{port} dir=in action=allow protocol=UDP localport={port}; "
                "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
                f"& netsh.exe advfirewall firewall add rule name=Awesome_BAR_TCP_{port} dir=in action=allow protocol=TCP localport={port}; "
                "exit $LASTEXITCODE"
            )
            import base64
            encoded = base64.b64encode(elevated_script.encode("utf-16le")).decode("ascii")
            ps_command = (
                f"$p = Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}' "
                "-Verb RunAs -Wait -PassThru; exit $p.ExitCode"
            )
            try:
                r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
                                   capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                raise UserError("Timed out waiting for Windows Firewall approval. Accept the UAC prompt and try again.")
            if r.returncode != 0:
                detail = (r.stderr or r.stdout).strip()
                raise UserError("Could not add the Windows Firewall rules. Accept the UAC prompt when it appears.\n"
                                f"{detail or 'The elevated netsh command did not succeed.'}")
            self.set_status(f"Windows Firewall rules added for inbound UDP and TCP port {port}.")
            return

        if shutil.which("ufw"):
            cmds = [["pkexec", "ufw", "allow", f"{port}/udp"], ["pkexec", "ufw", "allow", f"{port}/tcp"]]
        elif shutil.which("firewall-cmd"):
            cmds = [["pkexec", "firewall-cmd", f"--add-port={port}/udp"],
                    ["pkexec", "firewall-cmd", f"--add-port={port}/tcp"]]
        else:
            raise UserError(f"No ufw or firewalld was found. Allow inbound UDP and TCP port {port} manually. "
                            "UDP is for BAR; TCP is for the settings preview.")
        for cmd in cmds:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                raise UserError(f"Firewall command failed:\n{' '.join(cmd)}\n\n{(r.stderr or r.stdout).strip()}")
        note = " (firewalld rules are runtime-only; they clear on reload/reboot)" if cmds[0][1] == "firewall-cmd" else ""
        self.set_status(f"Firewall rules added for inbound UDP and TCP port {port}.{note}")

    # ----- lobby presets (save/load everything) -----
    def export_state(self):
        rows = []
        for s in self.players.slots():
            rows.append({"kind": s["kind"], "name": s["name"], "password": s["password"], "ally": s["ally"],
                         "ai": s["ai_text"], "bonus": s["bonus"], "side": s["side"], "color": list(s["color"])})
        return {"version": 2, "shared_password": self.tb_host_password.text(),
                "map": self.tb_map.currentText().strip(), "port": self.tb_host_port.text().strip(),
                "startpos": self.cb_startpos.currentData(), "startbox": self.cb_startbox.currentData(),
                "custom_boxes": self.tb_custom_boxes.toPlainText(), "min_speed": self.tb_min_speed.text(),
                "max_speed": self.tb_max_speed.text(), "start_delay": self.tb_start_delay.text(), "seed": self.tb_seed.text(),
                "no_damage": self.chk_no_damage.isChecked(), "players": rows,
                "game_options": self.game_panel.values(True), "map_options": self.map_panel.values(True)}

    def import_state(self, d):
        self.tb_map.setEditText(d.get("map", self.tb_map.currentText()))
        self.tb_host_port.setText(str(d.get("port", "8452")))
        for combo, key in ((self.cb_startpos, "startpos"), (self.cb_startbox, "startbox")):
            idx = combo.findData(d.get(key))
            if idx >= 0:
                combo.setCurrentIndex(idx)
        self.tb_custom_boxes.setPlainText(d.get("custom_boxes", ""))
        self.tb_min_speed.setText(d.get("min_speed", ""))
        self.tb_max_speed.setText(d.get("max_speed", ""))
        self.tb_start_delay.setText(d.get("start_delay", ""))
        self.tb_seed.setText(d.get("seed", ""))
        self.chk_no_damage.setChecked(bool(d.get("no_damage", False)))
        players = d.get("players") or []
        # Version-1 presets had individual passwords; migrate the first one found to
        # the new shared password setting.
        shared_password = d.get("shared_password")
        if shared_password is None:
            shared_password = next((p.get("password", "") for p in players[1:]
                                    if p.get("kind", "human") in ("human", "spec") and p.get("password")), "")
        self.tb_host_password.setText(str(shared_password or ""))
        if players:
            self.players.clear_non_host()
            self.tb_host_name.setText(players[0].get("name", self.tb_host_name.text()))
            hr = self.players.rows[0]
            hr["team"].setValue(int(players[0].get("ally", 1)))
            hr["bonus"].setValue(int(players[0].get("bonus", 0)))
            if players[0].get("side"):
                side_index = hr["side"].findText(str(players[0]["side"]))
                if side_index >= 0:
                    hr["side"].setCurrentIndex(side_index)
            if players[0].get("color"):
                hr["color"] = tuple(players[0]["color"])
                self.players.paint_color(hr)
            for p in players[1:]:
                try:
                    self.players.add_row(p.get("kind", "human"), name=p.get("name", ""), password=p.get("password", ""),
                                         ally=int(p.get("ally", 1)), ai=p.get("ai") or None, bonus=int(p.get("bonus", 0)),
                                         side=p.get("side") or None, color=p.get("color"))
                except UserError:
                    break
        self.game_panel.set_values(d.get("game_options", {}))
        self.map_panel.set_values(d.get("map_options", {}))
        self.refresh_map_options()

    @guarded
    def on_save_preset(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save lobby preset", str(Path.home() / "bar_lobby_preset.json"), "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(self.export_state(), indent=2), encoding="utf-8")
            self.set_status(f"Lobby preset saved: {path}")

    @guarded
    def on_load_preset(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load lobby preset", str(Path.home()), "JSON (*.json)")
        if path:
            self.import_state(json.loads(Path(path).read_text(encoding="utf-8")))
            self.set_status(f"Lobby preset loaded: {path}")

    def restore_last_lobby(self):
        d = load_json(LAST_LOBBY_PATH, None)
        if isinstance(d, dict):
            try:
                self.import_state(d)
            except Exception:  # noqa: BLE001 - a stale/corrupt file must never block startup
                pass

    def closeEvent(self, event):
        try:
            self.lobby_snapshot_timer.stop()
            self.lobby_server_restart_timer.stop()
            self.stop_lobby_preview_server()
        except Exception:
            pass
        try:
            LAST_LOBBY_PATH.parent.mkdir(parents=True, exist_ok=True)
            LAST_LOBBY_PATH.write_text(json.dumps(self.export_state(), indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)

    # ----- map list, icons, and game/map options -----
    def refresh_map_options(self):
        name = self.tb_map.currentText().strip()
        opts = self.map_options_all.get(name, [])
        key = json.dumps([o["key"] for o in opts])
        if key == self._map_opts_key:
            return
        self._map_opts_key = key
        self.map_panel.set_options(opts)
        self.map_panel.set_empty_text("This map has no special map options." if self.game_info or self.map_options_all
                                      else "Map options are read from the maps in your BAR install. Click 'Refresh maps & options'.")

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
            self.pic_map.setPixmap(pm.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.pic_map.clear()
        if hasattr(self, "map_panel"):
            self.refresh_map_options()

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

    def import_game_info(self):
        self.game_info = load_json(MAPCACHE_DIR / "gameinfo.json", {})
        self.map_options_all = load_json(MAPCACHE_DIR / "mapoptions.json", {})
        opts = self.game_info.get("mod_options", [])
        self.game_panel.set_options(opts)
        if self.game_info.get("error"):
            self.game_panel.set_empty_text("Could not read the game's options: " + self.game_info["error"])
        elif not opts:
            self.game_panel.set_empty_text("No game options were found. Click 'Refresh maps & options' on the Match tab.")
        self.players.refresh_lists()
        self._map_opts_key = None
        self.refresh_map_options()

    def initial_map_load(self):
        if self.import_map_cache():
            self.set_status("Map list and options loaded from cache. Click Refresh maps & options after installing maps or updating BAR.")
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
        if IS_WINDOWS:
            path_value = env.value("PATH", "")
            env.insert("PATH", engine_dir + (os.pathsep + path_value if path_value else ""))
        else:
            ld = env.value("LD_LIBRARY_PATH", "")
            env.insert("LD_LIBRARY_PATH", engine_dir + (":" + ld if ld else ""))
        proc = QProcess(self)
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(engine_dir)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.finished.connect(self.on_scan_finished)
        self.scan_proc = proc
        self.scan_interactive = interactive
        self.set_status("Scanning installed maps and game options (the first scan can take a while)...")
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        proc.start(sys.executable, [os.path.abspath(__file__), "--scan", engine_dir, data, str(MAPCACHE_DIR)])

    def on_scan_finished(self, code, _status):
        QApplication.restoreOverrideCursor()
        proc, self.scan_proc = self.scan_proc, None
        out = bytes(proc.readAll()).decode("utf-8", "replace").strip()
        proc.deleteLater()
        lines = out.splitlines()
        last = lines[-1] if lines else ""
        if code == 0 and last.startswith("OK"):
            self.import_map_cache()
            self.import_game_info()
            warn = next((ln for ln in lines if ln.startswith("WARN")), "")
            self.set_status(f"Found {last.split()[-1]} installed maps and "
                            f"{len(self.game_info.get('mod_options', []))} game options." + (f" ({warn[5:]})" if warn else ""))
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
