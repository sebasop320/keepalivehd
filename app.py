#!/usr/bin/env python3
"""KeepAliveHD controller for Ubuntu Server.

One process does everything: scheduler, drive detection, keepalive reads,
SMART monitoring, web dashboard + JSON API, and a local control socket used
by the `keepalivehd` CLI.  Python standard library only.

Run by systemd as keepalivehd.service.  Helper flags (used by install.sh):
    app.py --init-config [...]   create /etc/keepalivehd/config.json if absent
    app.py --init-auth           create dashboard credentials if absent
    app.py --detect              show what the configured mountpoint resolves to
"""

import argparse
import collections
import copy
import datetime as dt
import glob
import hashlib
import hmac
import ipaddress
import json
import logging
import mmap
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
CONFIG_DIR = Path(os.environ.get("KEEPALIVEHD_CONFIG_DIR", "/etc/keepalivehd"))
STATE_DIR = Path(os.environ.get("KEEPALIVEHD_STATE_DIR", "/var/lib/keepalivehd"))
RUN_DIR = Path(os.environ.get("KEEPALIVEHD_RUN_DIR", "/run/keepalivehd"))
CONFIG_FILE = CONFIG_DIR / "config.json"
AUTH_FILE = CONFIG_DIR / "auth.json"
STATE_FILE = STATE_DIR / "state.json"
SOCKET_FILE = RUN_DIR / "control.sock"
SOCKET_GROUP = "keepalivehd"

RECHECK_SECONDS = 10          # how often to re-check a missing drive
MODES = ("on", "off", "auto")

DEFAULT_CONFIG = {
    "mountpoint": "/mnt/hdd",
    # "vid:pid" of the USB bridge (e.g. "0bda:9201").  If set, keepalive refuses
    # to touch a disk that is not behind that bridge.  "" disables the check.
    "expected_usb_id": "",
    "interval_seconds": 120,
    "schedule": {"start": "05:00", "end": "22:00"},
    "read_timeout_seconds": 45,
    # false: always read the first 4 KiB (same as your dd test).
    # true: cycle through 256 offsets (1 MiB apart) so the drive cannot answer
    #       from its own cache.  Costs a tiny head seek per read.
    "rotate_offsets": False,
    "log_successful_reads": True,
    "smart": {
        "device_type": "sat",          # smartctl -d value; "auto" to let smartctl guess
        "interval_seconds": 300,
        "poll_when_inactive": False,   # false = never query the drive outside active hours
    },
    "temperature": {
        "warn": 45,
        "critical": 50,
        "protect": 55,
        "protect_action": "none",      # "none" or "pause_keepalive"
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8787,
        "session_hours": 12,
        "allowed_networks": [
            "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
            "169.254.0.0/16",
        ],
    },
}

log = logging.getLogger("keepalivehd")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def atomic_write(path, text, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(path))


def run(cmd, timeout=10):
    env = dict(os.environ, LC_ALL="C")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"


TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def parse_hhmm(value):
    m = TIME_RE.match(str(value))
    if not m:
        raise ValueError(f"invalid time '{value}' (use HH:MM, 24-hour)")
    return int(m.group(1)) * 60 + int(m.group(2))


def fmt_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def in_window(start, end, minute_of_day):
    """True if minute_of_day is inside [start, end).  Handles midnight crossing.
    start == end means 'always active'."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s == e:
        return True
    if s < e:
        return s <= minute_of_day < e
    return minute_of_day >= s or minute_of_day < e


def validate_schedule(start, end, interval):
    s = fmt_hhmm(parse_hhmm(start))
    e = fmt_hhmm(parse_hhmm(end))
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        raise ValueError("interval must be a whole number of seconds")
    if not 10 <= interval <= 3600:
        raise ValueError("interval must be between 10 and 3600 seconds")
    return s, e, interval


def sanitize_config(cfg):
    """Return (config, warnings).  Never raises; falls back to defaults."""
    warnings = []
    try:
        s, e, i = validate_schedule(cfg["schedule"]["start"], cfg["schedule"]["end"],
                                    cfg["interval_seconds"])
        cfg["schedule"]["start"], cfg["schedule"]["end"], cfg["interval_seconds"] = s, e, i
    except (ValueError, KeyError, TypeError) as exc:
        warnings.append(f"invalid schedule/interval in config ({exc}); using defaults")
        cfg["schedule"] = dict(DEFAULT_CONFIG["schedule"])
        cfg["interval_seconds"] = DEFAULT_CONFIG["interval_seconds"]
    cfg["expected_usb_id"] = str(cfg.get("expected_usb_id") or "").strip().lower()
    if cfg["temperature"].get("protect_action") not in ("none", "pause_keepalive"):
        warnings.append("temperature.protect_action must be 'none' or 'pause_keepalive'; using 'none'")
        cfg["temperature"]["protect_action"] = "none"
    for key in ("warn", "critical", "protect"):
        try:
            cfg["temperature"][key] = int(cfg["temperature"][key])
        except (TypeError, ValueError):
            cfg["temperature"][key] = DEFAULT_CONFIG["temperature"][key]
    nets = []
    for n in cfg["web"].get("allowed_networks", []):
        try:
            nets.append(ipaddress.ip_network(n, strict=False))
        except ValueError:
            warnings.append(f"ignoring invalid allowed_networks entry '{n}'")
    cfg["_nets"] = nets
    return cfg, warnings


def load_config_file():
    raw = {}
    if CONFIG_FILE.exists():
        raw = json.loads(CONFIG_FILE.read_text())
    return raw


def temperature_level(temp, tcfg):
    if temp is None:
        return None
    if temp >= tcfg["protect"]:
        return "protect"
    if temp >= tcfg["critical"]:
        return "critical"
    if temp >= tcfg["warn"]:
        return "warn"
    return "ok"


def parse_smart(text):
    """Extract (temperature_celsius, health) from smartctl output."""
    temp = None
    best = None
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 10 and f[0] in ("190", "194") and f[0].isdigit():
            m = re.match(r"\d+", f[9])
            if m:
                val = int(m.group(0))
                if f[0] == "194" or best is None:
                    best = val
                    if f[0] == "194":
                        break
        m = re.match(r"\s*Current (?:Drive )?Temperature:\s+(\d+)", line)
        if m and best is None:
            best = int(m.group(1))
    temp = best
    health = None
    m = re.search(r"overall-health self-assessment test result:\s*(\w+)", text)
    if m:
        health = m.group(1)
    else:
        m = re.search(r"SMART Health Status:\s*(\w+)", text)
        if m:
            health = m.group(1)
    return temp, health


# --------------------------------------------------------------------------
# drive detection (never touches the disk: findmnt/lsblk/sysfs only)
# --------------------------------------------------------------------------

def usb_id_for_block(name):
    try:
        p = Path(os.path.realpath(f"/sys/block/{name}"))
    except OSError:
        return None
    for d in [p, *p.parents]:
        if str(d) in ("/sys", "/"):
            break
        v, pr = d / "idVendor", d / "idProduct"
        try:
            if v.exists() and pr.exists():
                return f"{v.read_text().strip()}:{pr.read_text().strip()}".lower()
        except OSError:
            return None
    return None


def usb_present(usb_id):
    for v in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            d = Path(v).parent
            if f"{(d / 'idVendor').read_text().strip()}:{(d / 'idProduct').read_text().strip()}".lower() == usb_id:
                return True
        except OSError:
            continue
    return False


def detect_disk(mountpoint, expected_usb=""):
    d = {"mountpoint": mountpoint, "mounted": False, "connected": None, "ready": False,
         "source": None, "device": None, "disk": None, "model": None, "usb_id": None,
         "size_bytes": None, "problem": None}
    rc, out, _ = run(["findmnt", "-n", "-o", "SOURCE", "--mountpoint", mountpoint])
    src = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""
    if not src:
        d["problem"] = f"{mountpoint} is not mounted"
        if expected_usb:
            d["connected"] = usb_present(expected_usb)
        return d
    d["mounted"] = True
    src = re.sub(r"\[.*\]$", "", src)          # bind mounts: /dev/sda1[/sub]
    d["source"] = src
    if not src.startswith("/dev/"):
        d["problem"] = f"mount source '{src}' is not a block device"
        return d
    real = os.path.realpath(src)
    rc, out, err = run(["lsblk", "-nsrpo", "NAME,TYPE", real])
    if rc != 0:
        d["connected"] = False
        d["problem"] = f"device {real} is gone or unreadable ({err.strip() or 'lsblk failed'})"
        return d
    disks = list(dict.fromkeys(
        ln.split()[0] for ln in out.splitlines()
        if len(ln.split()) >= 2 and ln.split()[1] == "disk"))
    if len(disks) != 1:
        d["problem"] = ("could not resolve a single physical disk behind "
                        f"{mountpoint} (found {len(disks)})")
        return d
    dev = disks[0]
    name = os.path.basename(dev)
    sysdir = Path(f"/sys/block/{name}")
    try:
        mode = os.stat(dev).st_mode
    except OSError:
        mode = 0
    if not sysdir.exists() or not stat.S_ISBLK(mode):
        d["connected"] = False
        d["problem"] = f"{dev} does not exist (drive disconnected?)"
        return d
    try:
        size = int((sysdir / "size").read_text().strip()) * 512
    except (OSError, ValueError):
        size = 0
    d["device"], d["disk"], d["size_bytes"] = dev, name, size
    if size <= 0:
        d["connected"] = False
        d["problem"] = f"{dev} reports zero size (drive disconnected?)"
        return d
    d["connected"] = True
    try:
        d["model"] = (sysdir / "device" / "model").read_text().strip()
    except OSError:
        pass
    d["usb_id"] = usb_id_for_block(name)
    if expected_usb and d["usb_id"] != expected_usb:
        d["problem"] = (f"{dev} is behind USB id {d['usb_id'] or 'n/a'}, expected "
                        f"{expected_usb}; refusing to touch it")
        return d
    d["ready"] = True
    return d


def direct_read(device, offset, length=4096):
    """One small O_DIRECT read: bypasses the page cache, never writes."""
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECT", 0)
    fd = os.open(device, flags)
    try:
        buf = mmap.mmap(-1, length)          # page aligned, as O_DIRECT requires
        try:
            n = os.preadv(fd, [buf], offset)
        finally:
            buf.close()
        if n <= 0:
            raise OSError("zero-length read")
        return n
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------

class Auth:
    ITER = 200_000

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.sessions = {}
        self.fails = {}
        self._data = None
        self._mtime = None

    def _load(self):
        try:
            m = self.path.stat().st_mtime
        except OSError:
            self._data, self._mtime = None, None
            return None
        if m != self._mtime:
            try:
                self._data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self._data = None
            self._mtime = m
        return self._data

    def exists(self):
        with self.lock:
            return self._load() is not None

    @staticmethod
    def _hash(password, salt, iters):
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters).hex()

    def set_password(self, username, password):
        salt = secrets.token_bytes(16)
        data = {"username": username, "salt": salt.hex(), "iterations": self.ITER,
                "hash": self._hash(password, salt, self.ITER)}
        with self.lock:
            atomic_write(self.path, json.dumps(data), 0o600)
            self._data, self._mtime = data, self.path.stat().st_mtime
            self.sessions.clear()

    def username(self):
        with self.lock:
            d = self._load()
            return d["username"] if d else "admin"

    def verify(self, username, password):
        with self.lock:
            d = self._load()
        if not d:
            return False
        h = self._hash(password, bytes.fromhex(d["salt"]), int(d["iterations"]))
        ok_pw = hmac.compare_digest(h, d["hash"])
        ok_user = hmac.compare_digest(username.encode(), d["username"].encode())
        return ok_pw and ok_user

    # -- brute-force throttling (per client IP)
    def locked_for(self, ip):
        with self.lock:
            rec = self.fails.get(ip)
            if rec and rec[1] > time.time():
                return int(rec[1] - time.time()) + 1
        return 0

    def record_failure(self, ip):
        with self.lock:
            cnt = self.fails.get(ip, [0, 0])[0] + 1
            lock_until = time.time() + min(900, 30 * 2 ** (cnt - 5)) if cnt >= 5 else 0
            self.fails[ip] = [cnt, lock_until]
            if len(self.fails) > 500:
                self.fails.clear()

    def record_success(self, ip):
        with self.lock:
            self.fails.pop(ip, None)

    # -- sessions
    def new_session(self, hours):
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.sessions = {t: e for t, e in self.sessions.items() if e > now}
            self.sessions[token] = now + hours * 3600
        return token

    def valid(self, token):
        if not token:
            return False
        with self.lock:
            exp = self.sessions.get(token)
            if exp and exp > time.time():
                return True
            self.sessions.pop(token, None)
        return False

    def drop(self, token):
        with self.lock:
            self.sessions.pop(token, None)


# --------------------------------------------------------------------------
# controller
# --------------------------------------------------------------------------

class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.smart_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.reload_flag = threading.Event()
        self.events = collections.deque(maxlen=400)
        self.started = time.time()
        self.httpd = None
        self.auth = None

        self.config = {}
        self.reload_config(initial=True)
        self.state = self._load_state()

        self.disk = {"mountpoint": self.config["mountpoint"], "mounted": False,
                     "connected": None, "ready": False, "problem": "not checked yet",
                     "checked_at": 0}
        self.disk_ready_prev = None
        self.last_device = None
        self.want = False
        self.reason = "starting"
        self.active_prev = None
        self.paused = False
        self.next_read = 0.0
        self.next_smart = 0.0
        self.read_counter = 0
        self.consec_failures = 0
        self.inflight = None
        self.last_error = None
        self.last_read_ms = None
        self.smart = {"temp": None, "health": None, "power": "unknown", "at": 0,
                      "level": None, "failed": False}
        self._last_saved = time.time()
        self._tz_tick = 0

    # ---- logging -----------------------------------------------------
    def ev(self, level, msg):
        py = {"ok": logging.INFO, "info": logging.INFO,
              "warning": logging.WARNING, "error": logging.ERROR}[level]
        log.log(py, msg)
        self.events.append({"ts": time.time(), "level": level, "msg": msg})
        if level == "error":
            self.last_error = {"ts": time.time(), "msg": msg}

    # ---- config / state ------------------------------------------------
    def reload_config(self, initial=False):
        try:
            raw = load_config_file()
        except (OSError, ValueError) as exc:
            if not initial:
                self.ev("error", f"Config reload failed ({exc}); keeping previous settings")
                return
            raw = {}
            logging.basicConfig()
            log.error("Cannot parse %s (%s); using defaults", CONFIG_FILE, exc)
        cfg, warns = sanitize_config(deep_merge(DEFAULT_CONFIG, raw))
        with self.lock:
            self.config = cfg
            if not initial:
                self.next_read = 0
                self.next_smart = 0
        for w in warns:
            if initial:
                log.warning(w)
            else:
                self.ev("warning", w)
        if not initial:
            self.ev("info", "Configuration reloaded")
            self.wake.set()

    def _load_state(self):
        st = {"mode": "auto", "last_ok": None}
        try:
            data = json.loads(STATE_FILE.read_text())
            if data.get("mode") in MODES:
                st["mode"] = data["mode"]
            if isinstance(data.get("last_ok"), (int, float)):
                st["last_ok"] = data["last_ok"]
        except (OSError, ValueError):
            pass
        return st

    def save_state(self):
        try:
            with self.lock:
                text = json.dumps(self.state)
            atomic_write(STATE_FILE, text, 0o640)
            self._last_saved = time.time()
        except OSError as exc:
            self.ev("warning", f"Could not save state: {exc}")

    # ---- decisions -----------------------------------------------------
    def desired(self):
        mode = self.state["mode"]
        if mode == "on":
            return True, "manual ON (schedule ignored)"
        if mode == "off":
            return False, "manual OFF (schedule ignored)"
        now = dt.datetime.now()
        sch = self.config["schedule"]
        inside = in_window(sch["start"], sch["end"], now.hour * 60 + now.minute)
        if inside:
            return True, f"AUTO: inside schedule {sch['start']}-{sch['end']}"
        return False, f"AUTO: outside schedule {sch['start']}-{sch['end']}"

    def client_allowed(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in self.config["_nets"])

    # ---- public operations (HTTP + CLI) --------------------------------
    def set_mode(self, mode, source):
        mode = str(mode).lower()
        if mode not in MODES:
            raise ValueError("mode must be one of on, off, auto")
        with self.lock:
            self.state["mode"] = mode
            self.next_read = 0
            self.paused = False
        self.save_state()
        self.ev("info", f"Mode set to {mode.upper()} via {source}")
        want, reason = self.desired()
        with self.lock:
            self.want, self.reason = want, reason      # status reflects it immediately
        self.wake.set()

    def get_schedule(self):
        with self.lock:
            c = self.config
            return {"start": c["schedule"]["start"], "end": c["schedule"]["end"],
                    "interval_seconds": c["interval_seconds"]}

    def set_schedule(self, start, end, interval, source):
        s, e, i = validate_schedule(start, end, interval)
        raw = load_config_file()
        raw.setdefault("schedule", {})
        raw["schedule"]["start"], raw["schedule"]["end"] = s, e
        raw["interval_seconds"] = i
        atomic_write(CONFIG_FILE, json.dumps(raw, indent=2) + "\n", 0o644)
        with self.lock:
            self.config["schedule"]["start"] = s
            self.config["schedule"]["end"] = e
            self.config["interval_seconds"] = i
            self.next_read = 0
        self.ev("info", f"Schedule set to {s}-{e}, interval {i}s via {source}")
        self.wake.set()

    def get_logs(self, level="events", limit=100):
        keep = {"errors": ("warning", "error"),
                "events": ("info", "warning", "error"),
                "all": ("ok", "info", "warning", "error")}.get(level, ("info", "warning", "error"))
        items = [e for e in list(self.events) if e["level"] in keep]
        return items[-max(1, min(int(limit), 400)):][::-1]

    def change_password(self, password=None):
        pw = password or secrets.token_urlsafe(12)
        if len(pw) < 8:
            raise ValueError("password must be at least 8 characters")
        user = self.auth.username()
        self.auth.set_password(user, pw)
        self.ev("info", "Dashboard password changed (all web sessions signed out)")
        return user, pw

    def standby_now(self, source):
        """Switch to OFF and spin the drive down immediately (STANDBY IMMEDIATE)."""
        disk = self.refresh_disk(0)
        if not disk.get("ready"):
            raise ValueError(f"drive not available: {disk.get('problem')}")
        prev = self.state["mode"]
        self.set_mode("off", source)               # otherwise the next keepalive read would wake it
        th = self.inflight
        if th is not None and th.is_alive():
            th.join(5)
        scfg = self.config["smart"]
        dtype = ["-d", scfg["device_type"]] if scfg["device_type"] and scfg["device_type"] != "auto" else []
        dev = disk["device"]
        rc, out, err = run(["smartctl", *dtype, "-s", "standby,now", dev], timeout=30)
        tool = "smartctl"
        ok = rc == 0 and "STANDBY" in out.upper()
        detail = (err.strip() or out.strip() or "no output").splitlines()[-1] if not ok else ""
        if not ok and shutil.which("hdparm"):
            rc, out2, err2 = run(["hdparm", "-y", dev], timeout=30)
            tool = "hdparm"
            ok = rc == 0
            if not ok:
                detail = (err2.strip() or out2.strip() or detail).splitlines()[-1]
        if not ok:
            self.set_mode(prev, source)            # do not leave the user in OFF after a failure
            self.ev("error", f"Standby command failed on {dev}: {detail}")
            raise ValueError(f"standby command failed: {detail}")
        with self.lock:
            self.smart["power"] = "standby"
            self.smart["at"] = time.time()
        self.ev("info", f"Standby command sent to {dev} via {tool}. Mode is OFF; "
                        "choose AUTO or ON to resume keepalive.")

    def probe(self):
        """Force a fresh drive check + SMART query now (may wake the drive)."""
        self.refresh_disk(0)
        self.poll_smart(force=True)

    # ---- disk ----------------------------------------------------------
    def refresh_disk(self, max_age):
        now = time.time()
        with self.lock:
            if now - self.disk.get("checked_at", 0) < max_age:
                return self.disk
            mp, usb = self.config["mountpoint"], self.config["expected_usb_id"]
        d = detect_disk(mp, usb)
        d["checked_at"] = time.time()
        with self.lock:
            self.disk = d
        return d

    def note_disk_state(self, disk):
        ready = disk["ready"]
        with self.lock:
            prev = self.disk_ready_prev
            self.disk_ready_prev = ready
            if ready:
                self.next_read = min(self.next_read, time.time()) if prev is False else self.next_read
        if ready and prev is not True:
            if prev is False:
                self.ev("info", f"HDD available again ({disk['device']}); resuming keepalive")
            else:
                self.ev("info", f"HDD detected: {disk['device']} ({disk.get('model') or 'unknown model'}) "
                                f"mounted at {disk['mountpoint']}")
        elif not ready and prev is not False:
            self.ev("warning", f"HDD not available: {disk['problem']}. "
                               "Keepalive suspended; checking again every "
                               f"{RECHECK_SECONDS}s.")
        if ready and self.last_device and self.last_device != disk["device"]:
            self.ev("info", f"HDD device name changed {self.last_device} -> {disk['device']}")
        if ready:
            self.last_device = disk["device"]

    # ---- keepalive -----------------------------------------------------
    def do_keepalive(self):
        disk = self.refresh_disk(0)
        self.note_disk_state(disk)
        now = time.time()
        if not disk["ready"]:
            self.next_read = now + RECHECK_SECONDS
            return
        if self.inflight is not None and self.inflight.is_alive():
            self.next_read = now + 5
            return

        cfg = self.config
        offset = 0
        if cfg["rotate_offsets"]:
            offset = (self.read_counter % 256) * 1024 * 1024
            if disk["size_bytes"] and offset + 4096 > disk["size_bytes"]:
                offset = 0
        self.read_counter += 1

        result = {}
        device = disk["device"]

        def work():
            try:
                result["bytes"] = direct_read(device, offset)
            except Exception as exc:        # noqa: BLE001
                result["error"] = f"{type(exc).__name__}: {exc}"

        t0 = time.time()
        th = threading.Thread(target=work, name="keepalive-read", daemon=True)
        self.inflight = th
        th.start()
        th.join(cfg["read_timeout_seconds"])
        now = time.time()
        elapsed_ms = (now - t0) * 1000

        if th.is_alive():
            self._read_failed(f"read of {device} did not finish within "
                              f"{cfg['read_timeout_seconds']}s (drive stuck or spinning up?)")
            return
        if "error" in result:
            self._read_failed(f"read of {device} failed: {result['error']}")
            return

        with self.lock:
            recovered = self.consec_failures > 0
            self.consec_failures = 0
            self.state["last_ok"] = now
            self.last_read_ms = elapsed_ms
            self.next_read = now + cfg["interval_seconds"]
        if recovered:
            self.ev("info", "Keepalive reads are working again")
        if cfg["log_successful_reads"]:
            note = " (drive was probably spinning up)" if elapsed_ms > 3000 else ""
            self.ev("ok", f"Keepalive read OK: {device} offset {offset}, {elapsed_ms:.0f} ms{note}")
        if now - self._last_saved > 900:
            self.save_state()
        if now >= self.next_smart:
            self.poll_smart()
            self.next_smart = time.time() + cfg["smart"]["interval_seconds"]

    def _read_failed(self, msg):
        with self.lock:
            self.consec_failures += 1
            n = self.consec_failures
            self.next_read = time.time() + (15 if n <= 3 else self.config["interval_seconds"])
        self.ev("error", f"Keepalive {msg} (failure #{n})")

    # ---- SMART ---------------------------------------------------------
    def poll_smart(self, force=False):
        with self.smart_lock:
            disk = self.disk
            if not disk.get("ready"):
                return
            scfg = self.config["smart"]
            cmd = ["smartctl", "-n", "standby", "-A", "-H"]
            if scfg["device_type"] and scfg["device_type"] != "auto":
                cmd += ["-d", scfg["device_type"]]
            cmd.append(disk["device"])
            rc, out, err = run(cmd, timeout=40)
            now = time.time()
            s = dict(self.smart)
            s["at"] = now
            if re.search(r"Device is in (STANDBY|SLEEP)", out):
                s["power"] = "standby"
                s["failed"] = False
            else:
                temp, health = parse_smart(out)
                if temp is None and health is None:
                    if not s.get("failed"):
                        first = (err.strip() or out.strip() or "no output").splitlines()[0]
                        self.ev("warning", f"SMART query failed (exit {rc}): {first}")
                    s["failed"] = True
                    s["power"] = "unknown"
                else:
                    s.update(failed=False, power="active/idle", health=health)
                    if temp is not None:
                        s["temp"] = temp
            with self.lock:
                self.smart = s
            self._evaluate_temperature()

    def _evaluate_temperature(self):
        tcfg = self.config["temperature"]
        with self.lock:
            temp = self.smart.get("temp")
            prev = self.smart.get("level")
            level = temperature_level(temp, tcfg)
            self.smart["level"] = level
        if level != prev and level is not None:
            if level == "ok":
                if prev:
                    self.ev("info", f"HDD temperature back to normal ({temp} C)")
            elif level == "warn":
                self.ev("warning", f"HDD temperature {temp} C (warning threshold {tcfg['warn']} C)")
            else:
                self.ev("error", f"HDD temperature {temp} C is {level.upper()} "
                                 f"(threshold {tcfg['critical' if level == 'critical' else 'protect']} C)")
        if tcfg["protect_action"] == "pause_keepalive":
            if level == "protect" and not self.paused:
                self.paused = True
                self.ev("error", f"Keepalive PAUSED: {temp} C >= {tcfg['protect']} C. "
                                 "The drive may now go to standby and cool down.")
            elif self.paused and (
                    self.smart.get("power") == "standby"
                    or (temp is not None and temp <= tcfg["protect"] - 3)):
                self.paused = False
                self.next_read = 0
                self.ev("info", "Temperature recovered; keepalive resumed")

    # ---- main loop -----------------------------------------------------
    def run(self):
        self.ev("info", f"KeepAliveHD {VERSION} started (mode {self.state['mode'].upper()}, "
                        f"mountpoint {self.config['mountpoint']})")
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:               # noqa: BLE001
                log.exception("Unexpected error in controller loop")
                self.ev("error", "Unexpected internal error (see journal); continuing")
                self.stop.wait(5)
            self.wake.wait(1.0)
            self.wake.clear()
        self.save_state()

    def tick(self):
        if self.reload_flag.is_set():
            self.reload_flag.clear()
            self.reload_config()
        self._tz_tick += 1
        if self._tz_tick % 60 == 1:
            time.tzset()                    # pick up `timedatectl set-timezone`

        now = time.time()
        want, reason = self.desired()
        with self.lock:
            if want != self.active_prev:
                self.active_prev = want
                self.ev("info", f"Keepalive {'STARTED' if want else 'STOPPED'}: {reason}")
                self.next_read = 0
                self.next_smart = 0
                self.disk_ready_prev = None if want else self.disk_ready_prev
            self.want, self.reason = want, reason
        scfg = self.config["smart"]

        if not want:
            self.refresh_disk(120)          # sysfs/findmnt only: never touches the drive
            if scfg["poll_when_inactive"] and now >= self.next_smart:
                self.poll_smart()
                self.next_smart = time.time() + scfg["interval_seconds"]
            return
        if self.paused:
            self.refresh_disk(30)
            if now >= self.next_smart:
                self.poll_smart()
                self.next_smart = time.time() + scfg["interval_seconds"]
            return
        if now >= self.next_read:
            self.do_keepalive()
        else:
            self.refresh_disk(30)

    # ---- status --------------------------------------------------------
    def get_status(self):
        now = time.time()
        with self.lock:
            disk = dict(self.disk)
            smart = dict(self.smart)
            cfg = self.config
            want, reason, paused = self.want, self.reason, self.paused
            last_ok = self.state.get("last_ok")
            if not want:
                ka_state = "idle"
            elif paused:
                ka_state = "paused"
            elif not disk.get("ready"):
                ka_state = "waiting"
            else:
                ka_state = "active"
            polled = smart["at"] > 0
            power = smart["power"] if polled else "unknown"
            if ka_state == "active" and last_ok and now - last_ok < cfg["interval_seconds"] * 2 \
                    and power == "unknown":
                power = "active/idle"
            local = dt.datetime.now().astimezone()
            return {
                "version": VERSION,
                "server_time": local.isoformat(timespec="seconds"),
                "timezone": local.tzname(),
                "uptime_seconds": int(now - self.started),
                "mode": self.state["mode"],
                "keepalive": {
                    "state": ka_state,
                    "desired": want,
                    "reason": reason,
                    "interval_seconds": cfg["interval_seconds"],
                    "last_ok": last_ok,
                    "last_ok_age": int(now - last_ok) if last_ok else None,
                    "last_read_ms": self.last_read_ms,
                    "next_read_in": max(0, int(self.next_read - now)) if ka_state == "active" else None,
                    "consecutive_failures": self.consec_failures,
                    "last_error": self.last_error,
                },
                "schedule": {"start": cfg["schedule"]["start"], "end": cfg["schedule"]["end"],
                             "interval_seconds": cfg["interval_seconds"]},
                "disk": {k: disk.get(k) for k in
                         ("mountpoint", "mounted", "connected", "ready", "device", "model",
                          "usb_id", "problem", "size_bytes")},
                "smart": {
                    "polled": polled,
                    "polled_age": int(now - smart["at"]) if polled else None,
                    "temperature": smart["temp"],
                    "temperature_level": smart["level"],
                    "health": smart["health"],
                    "power_state": power,
                },
                "thresholds": {k: cfg["temperature"][k] for k in ("warn", "critical", "protect")},
                "protect_action": cfg["temperature"]["protect_action"],
            }


# --------------------------------------------------------------------------
# web server
# --------------------------------------------------------------------------

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
COOKIE = "kah_session"


def make_handler(ctl, auth):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KeepAliveHD"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 15

        def log_message(self, fmt, *args):      # keep the journal quiet
            pass

        # ---- plumbing
        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj, extra=None):
            self._send(code, json.dumps(obj).encode(), "application/json", extra)

        def _gate(self):
            ip = self.client_address[0]
            if not ctl.client_allowed(ip):
                self._json(403, {"error": "forbidden: address not in allowed_networks"})
                return False
            return True

        def _token(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c[COOKIE].value if COOKIE in c else None

        def _authed(self):
            return auth.valid(self._token())

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if n < 0 or n > 8192:
                self._json(413, {"error": "bad or too large request body"})
                return None
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
                return data if isinstance(data, dict) else {}
            except ValueError:
                self._json(400, {"error": "invalid JSON"})
                return None

        # ---- GET
        def do_GET(self):
            if not self._gate():
                return
            url = urlparse(self.path)
            path = url.path
            if path in STATIC:
                fname, ctype = STATIC[path]
                try:
                    self._send(200, (WEB_DIR / fname).read_bytes(), ctype)
                except OSError:
                    self._json(500, {"error": "web files missing"})
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if path == "/api/session":
                self._json(200, {"authenticated": self._authed(),
                                 "configured": auth.exists()})
                return
            if not path.startswith("/api/"):
                self._json(404, {"error": "not found"})
                return
            if not self._authed():
                self._json(401, {"error": "authentication required"})
                return
            if path == "/api/status":
                self._json(200, ctl.get_status())
            elif path == "/api/schedule":
                self._json(200, ctl.get_schedule())
            elif path == "/api/logs":
                q = parse_qs(url.query)
                self._json(200, {"logs": ctl.get_logs(
                    q.get("level", ["events"])[0], q.get("limit", ["100"])[0])})
            else:
                self._json(404, {"error": "not found"})

        # ---- POST
        def do_POST(self):
            if not self._gate():
                return
            path = urlparse(self.path).path
            if self.headers.get("X-Requested-With") != "keepalivehd":
                self._json(403, {"error": "missing X-Requested-With header"})
                return
            body = self._body()
            if body is None:
                return
            ip = self.client_address[0]

            if path == "/api/login":
                wait = auth.locked_for(ip)
                if wait:
                    self._json(429, {"error": f"too many attempts; try again in {wait}s"},
                               {"Retry-After": str(wait)})
                    return
                if auth.verify(str(body.get("username", "")), str(body.get("password", ""))):
                    auth.record_success(ip)
                    hours = ctl.config["web"]["session_hours"]
                    tok = auth.new_session(hours)
                    self._json(200, {"ok": True}, {
                        "Set-Cookie": f"{COOKIE}={tok}; HttpOnly; SameSite=Strict; Path=/; "
                                      f"Max-Age={int(hours * 3600)}"})
                else:
                    auth.record_failure(ip)
                    ctl.ev("warning", f"Failed dashboard login from {ip}")
                    time.sleep(0.5)
                    self._json(401, {"error": "invalid username or password"})
                return
            if path == "/api/logout":
                auth.drop(self._token())
                self._json(200, {"ok": True}, {
                    "Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
                return
            if not self._authed():
                self._json(401, {"error": "authentication required"})
                return
            try:
                if path == "/api/mode":
                    ctl.set_mode(body.get("mode", ""), f"web ({ip})")
                    self._json(200, ctl.get_status())
                elif path == "/api/schedule":
                    ctl.set_schedule(body.get("start", ""), body.get("end", ""),
                                     body.get("interval_seconds", ""), f"web ({ip})")
                    self._json(200, ctl.get_schedule())
                elif path == "/api/standby":
                    ctl.standby_now(f"web ({ip})")
                    self._json(200, ctl.get_status())
                elif path == "/api/probe":
                    ctl.probe()
                    self._json(200, ctl.get_status())
                else:
                    self._json(404, {"error": "not found"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except OSError as exc:
                self._json(500, {"error": f"could not save: {exc}"})

    return Handler


def web_thread(ctl, auth):
    handler = make_handler(ctl, auth)
    web = ctl.config["web"]
    httpd = None
    while not ctl.stop.is_set():
        try:
            httpd = ThreadingHTTPServer((web["host"], int(web["port"])), handler)
            break
        except OSError as exc:
            ctl.ev("error", f"Cannot bind dashboard on {web['host']}:{web['port']}: {exc}. "
                            "Retrying in 30s (keepalive itself is unaffected).")
            ctl.stop.wait(30)
    if httpd is None:
        return
    httpd.daemon_threads = True
    ctl.httpd = httpd
    ctl.ev("info", f"Dashboard listening on http://{web['host']}:{web['port']}")
    httpd.serve_forever(poll_interval=1.0)


# --------------------------------------------------------------------------
# local control socket (used by the `keepalivehd` CLI)
# --------------------------------------------------------------------------

def handle_control(ctl, req, peer_uid):
    cmd = req.get("cmd")
    if cmd == "status":
        if req.get("probe"):
            ctl.probe()
        return {"ok": True, "status": ctl.get_status()}
    if cmd == "mode":
        ctl.set_mode(req.get("mode", ""), f"CLI (uid {peer_uid})")
        return {"ok": True, "status": ctl.get_status()}
    if cmd == "standby":
        ctl.standby_now(f"CLI (uid {peer_uid})")
        return {"ok": True, "status": ctl.get_status()}
    if cmd == "schedule":
        return {"ok": True, "schedule": ctl.get_schedule()}
    if cmd == "schedule_set":
        ctl.set_schedule(req.get("start", ""), req.get("end", ""),
                         req.get("interval_seconds", ctl.config["interval_seconds"]),
                         f"CLI (uid {peer_uid})")
        return {"ok": True, "schedule": ctl.get_schedule()}
    if cmd == "logs":
        return {"ok": True, "logs": ctl.get_logs(req.get("level", "events"), req.get("limit", 50))}
    if cmd == "passwd":
        if peer_uid not in (0, os.geteuid()):
            return {"ok": False, "error": "only root can change the dashboard password (use sudo)"}
        user, pw = ctl.change_password(req.get("password"))
        return {"ok": True, "username": user, "password": None if req.get("password") else pw}
    return {"ok": False, "error": f"unknown command '{cmd}'"}


def control_thread(ctl):
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            SOCKET_FILE.unlink()
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCKET_FILE))
        os.chmod(str(SOCKET_FILE), 0o660)
        try:
            import grp
            os.chown(str(SOCKET_FILE), 0, grp.getgrnam(SOCKET_GROUP).gr_gid)
        except (KeyError, PermissionError, ImportError):
            pass
        srv.listen(8)
        srv.settimeout(1.0)
    except OSError as exc:
        ctl.ev("error", f"Cannot create control socket {SOCKET_FILE}: {exc}")
        return
    while not ctl.stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with conn:
            try:
                conn.settimeout(60)
                data = b""
                while b"\n" not in data and len(data) < 8192:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                _, uid, _ = struct.unpack("3i", conn.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                try:
                    resp = handle_control(ctl, json.loads(data.decode() or "{}"), uid)
                except ValueError as exc:
                    resp = {"ok": False, "error": str(exc)}
                conn.sendall(json.dumps(resp).encode() + b"\n")
            except (OSError, socket.timeout):
                pass
    try:
        srv.close()
        SOCKET_FILE.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def cmd_init_config(args):
    if CONFIG_FILE.exists():
        print(f"kept existing {CONFIG_FILE}")
        return 0
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if args.mountpoint:
        cfg["mountpoint"] = args.mountpoint
    start = args.start or cfg["schedule"]["start"]
    end = args.end or cfg["schedule"]["end"]
    interval = args.interval or cfg["interval_seconds"]
    try:
        cfg["schedule"]["start"], cfg["schedule"]["end"], cfg["interval_seconds"] = \
            validate_schedule(start, end, interval)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.port:
        cfg["web"]["port"] = args.port
    d = detect_disk(cfg["mountpoint"], "")
    if d.get("usb_id"):
        cfg["expected_usb_id"] = d["usb_id"]
        print(f"detected {d['device']} behind USB bridge {d['usb_id']}; "
              "keepalive will only target a disk behind that bridge")
    else:
        print(f"note: could not detect the USB bridge of {cfg['mountpoint']} "
              f"({d.get('problem') or 'not USB'}); expected_usb_id left empty")
    atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2) + "\n", 0o644)
    print(f"wrote {CONFIG_FILE}")
    return 0


def cmd_init_auth(args):
    auth = Auth(AUTH_FILE)
    if auth.exists():
        return 0                                # keep existing credentials, print nothing
    pw = secrets.token_urlsafe(12)
    auth.set_password(args.username, pw)
    print(pw)
    return 0


def cmd_detect(_args):
    cfg, _ = sanitize_config(deep_merge(DEFAULT_CONFIG, load_config_file()))
    d = detect_disk(cfg["mountpoint"], cfg["expected_usb_id"])
    print(json.dumps(d, indent=2))
    return 0 if d["ready"] else 2


def run_daemon():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ctl = Controller()
    auth = Auth(AUTH_FILE)
    ctl.auth = auth
    if not auth.exists():
        ctl.ev("warning", "No dashboard credentials found; run: sudo keepalivehd passwd")

    signal.signal(signal.SIGTERM, lambda *_: (ctl.stop.set(), ctl.wake.set()))
    signal.signal(signal.SIGINT, lambda *_: (ctl.stop.set(), ctl.wake.set()))
    signal.signal(signal.SIGHUP, lambda *_: (ctl.reload_flag.set(), ctl.wake.set()))

    threading.Thread(target=web_thread, args=(ctl, auth), name="web", daemon=True).start()
    threading.Thread(target=control_thread, args=(ctl,), name="control", daemon=True).start()
    ctl.run()
    ctl.ev("info", "KeepAliveHD stopping")
    if ctl.httpd:
        threading.Thread(target=ctl.httpd.shutdown, daemon=True).start()
    try:
        SOCKET_FILE.unlink()
    except OSError:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser(description="KeepAliveHD controller")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--init-config", action="store_true")
    ap.add_argument("--init-auth", action="store_true")
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--mountpoint")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--interval", type=int)
    ap.add_argument("--port", type=int)
    ap.add_argument("--username", default="admin")
    args = ap.parse_args()
    if args.version:
        print(VERSION)
        return 0
    if args.init_config:
        return cmd_init_config(args)
    if args.init_auth:
        return cmd_init_auth(args)
    if args.detect:
        return cmd_detect(args)
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
