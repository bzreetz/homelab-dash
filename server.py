#!/usr/bin/env python3
"""
Homelab dashboard: a small, dependency-free web server that polls a Proxmox VE
host and an HPE iLO (Redfish) and serves a live dashboard to any browser.

    python3 server.py           # reads config.json next to this file
    python3 server.py --demo    # no server needed, shows simulated data

Only the Python 3 standard library is used, so it runs on a stock Debian LXC.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
CONFIG_PATH = os.environ.get("DASH_CONFIG", os.path.join(HERE, "config.json"))
DEMO = "--demo" in sys.argv

HISTORY_POINTS = 360        # one hour at one sample every 10 s
HISTORY_EVERY = 10


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)


# --------------------------------------------------------------------------- config

DEFAULTS = {
    "port": 8080,
    "bind": "0.0.0.0",
    "title": "",
    "admin_pin": "",
    "temp_unit": "C",
    "electricity_rate": 0,
    "currency": "$",
    "poll_seconds": 3,
    "devices": [],
    "proxmox": None,
    "ilo": None,
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            try:
                cfg.update(json.load(f))
            except json.JSONDecodeError as e:
                sys.exit(f"config.json has a typo on line {e.lineno}, column {e.colno}: {e.msg}")
    elif not DEMO:
        sys.exit(f"No config at {CONFIG_PATH}. Copy config.example.json to config.json and fill it in, "
                 f"or run with --demo to preview.")

    # A section left as the example placeholder counts as "not set up".
    required = {"proxmox": ("host", "token_id", "token_secret"), "ilo": ("host", "username", "password")}
    for key, fields in required.items():
        sect = cfg.get(key)
        if (not isinstance(sect, dict) or sect.get("enabled") is False
                or not all(sect.get(k) and "CHANGE" not in str(sect.get(k)) for k in fields)):
            cfg[key] = None
    cfg["admin_pin"] = str(cfg.get("admin_pin") or "")
    return cfg


# --------------------------------------------------------------------------- HTTP client

class UpstreamError(Exception):
    pass


def make_ssl_context(verify):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # iLO 4 and older Proxmox certs can trip OpenSSL 3's stricter defaults.
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return ctx


class Upstream:
    """Tiny JSON-over-HTTPS client. Never uses a proxy: these are LAN hosts."""

    def __init__(self, base, headers, verify=False, timeout=10, label="server"):
        self.base = base.rstrip("/")
        parsed = urllib.parse.urlparse(self.base)
        self.where = f"{parsed.scheme}://{parsed.netloc}"
        self.headers = headers
        self.timeout = timeout
        self.label = label
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=make_ssl_context(verify)),
        )

    def request(self, method, path, body=None, form=False):
        headers = {"Accept": "application/json", **self.headers}
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        elif method in ("POST", "PUT", "PATCH"):
            data = b""
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise UpstreamError(self.explain_http(e.code, path)) from None
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            raise UpstreamError(f"Can't reach {self.label} at {self.where} ({reason})") from None
        except (socket.timeout, TimeoutError):
            raise UpstreamError(f"{self.label} at {self.where} didn't answer within {self.timeout} s") from None
        except (ConnectionError, ssl.SSLError, OSError) as e:
            raise UpstreamError(f"Connection to {self.label} failed ({e})") from None
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise UpstreamError(f"{self.label} sent something that isn't JSON for {path}") from None

    def explain_http(self, code, path):
        if code == 401:
            return f"{self.label} rejected the login (401). Check the credentials in config.json."
        if code == 403:
            return f"{self.label} refused {path} (403). The account needs more permissions; see the README."
        if code == 404:
            return f"{self.label} has nothing at {path} (404)."
        return f"{self.label} returned HTTP {code} for {path}."


# --------------------------------------------------------------------------- Proxmox

def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Proxmox:
    def __init__(self, cfg):
        self.api = Upstream(
            cfg["host"].rstrip("/") + "/api2/json",
            {"Authorization": f"PVEAPIToken={cfg['token_id']}={cfg['token_secret']}"},
            verify=cfg.get("verify_ssl", False), timeout=10, label="Proxmox",
        )
        self.node = cfg.get("node") or None

    def get(self, path):
        return self.api.request("GET", path).get("data")

    def post(self, path, params=None):
        return self.api.request("POST", path, body=params or {}, form=True).get("data")

    def ensure_node(self):
        if not self.node:
            nodes = self.get("/nodes") or []
            if not nodes:
                raise UpstreamError("Proxmox returned no nodes")
            self.node = sorted(n.get("node", "") for n in nodes)[0]
        return self.node

    def poll_fast(self):
        node = self.ensure_node()
        status = self.get(f"/nodes/{node}/status") or {}
        resources = self.get("/cluster/resources") or []
        return self.normalize(node, status, resources)

    def poll_slow(self):
        node = self.ensure_node()
        rrd = self.get(f"/nodes/{node}/rrddata?timeframe=hour&cf=AVERAGE") or []
        try:
            disks = self.get(f"/nodes/{node}/disks/list") or []
        except UpstreamError:
            disks = None  # token lacks Sys.Audit on /nodes; the rest still works
        return self.normalize_rrd(rrd), self.normalize_disks(disks)

    @staticmethod
    def normalize(node, st, resources):
        mem, swap, root = st.get("memory") or {}, st.get("swap") or {}, st.get("rootfs") or {}
        cpuinfo = st.get("cpuinfo") or {}
        ver = st.get("pveversion") or ""
        m = re.search(r"pve-manager/([\d.]+)", ver)
        kernel = (st.get("current-kernel") or {}).get("release") or (st.get("kversion") or "").split(" ")[1:2]
        if isinstance(kernel, list):
            kernel = kernel[0] if kernel else None
        host = {
            "name": node,
            "uptime": st.get("uptime"),
            "cpu": num(st.get("cpu")),
            "iowait": num(st.get("wait")),
            "cpus": cpuinfo.get("cpus"),
            "sockets": cpuinfo.get("sockets"),
            "cores": cpuinfo.get("cores"),
            "cpu_model": re.sub(r"\s+", " ", cpuinfo.get("model") or "").strip() or None,
            "mhz": num(cpuinfo.get("mhz")),
            "loadavg": [num(x, 0) for x in (st.get("loadavg") or [])] or None,
            "mem_used": mem.get("used"),
            "mem_total": mem.get("total"),
            "swap_used": swap.get("used"),
            "swap_total": swap.get("total"),
            "root_used": root.get("used"),
            "root_total": root.get("total"),
            "pve_version": m.group(1) if m else (ver or None),
            "kernel": kernel,
        }

        guests, storage, nodes, seen_shared = [], [], [], set()
        for r in resources:
            kind = r.get("type")
            if kind in ("qemu", "lxc"):
                guests.append({
                    "vmid": r.get("vmid"),
                    "name": r.get("name") or f"{kind} {r.get('vmid')}",
                    "type": kind,
                    "node": r.get("node"),
                    "status": r.get("status"),
                    "template": bool(r.get("template")),
                    "cpu": num(r.get("cpu")),
                    "cpus": r.get("maxcpu"),
                    "mem": r.get("mem"),
                    "maxmem": r.get("maxmem"),
                    "disk": r.get("disk"),
                    "maxdisk": r.get("maxdisk"),
                    "uptime": r.get("uptime"),
                    "netin": r.get("netin"),
                    "netout": r.get("netout"),
                    "tags": [t for t in re.split(r"[;, ]+", r.get("tags") or "") if t],
                    "lock": r.get("lock"),
                })
            elif kind == "storage":
                key = r.get("storage")
                if r.get("shared"):
                    if key in seen_shared:
                        continue
                    seen_shared.add(key)
                storage.append({
                    "name": key,
                    "node": r.get("node"),
                    "plugin": r.get("plugintype"),
                    "content": [c for c in (r.get("content") or "").split(",") if c],
                    "used": r.get("disk"),
                    "total": r.get("maxdisk"),
                    "status": r.get("status"),
                    "shared": bool(r.get("shared")),
                })
            elif kind == "node":
                nodes.append({
                    "name": r.get("node"),
                    "online": r.get("status") == "online",
                    "cpu": num(r.get("cpu")),
                    "cpus": r.get("maxcpu"),
                    "mem": r.get("mem"),
                    "maxmem": r.get("maxmem"),
                    "uptime": r.get("uptime"),
                })
        guests.sort(key=lambda g: (g["template"], g["status"] != "running", g["vmid"] or 0))
        storage.sort(key=lambda s: s["name"] or "")
        nodes.sort(key=lambda n: n["name"] or "")
        return {"host": host, "guests": guests, "storage": storage, "nodes": nodes}

    @staticmethod
    def normalize_rrd(rrd):
        points = []
        for p in rrd:
            if p.get("cpu") is None or p.get("time") is None:
                continue
            memfrac = None
            if p.get("memtotal"):
                memfrac = (p.get("memused") or 0) / p["memtotal"]
            points.append([int(p["time"]), num(p.get("cpu")), memfrac,
                           num(p.get("netin"), 0), num(p.get("netout"), 0), num(p.get("iowait"))])
        return points

    @staticmethod
    def normalize_disks(disks):
        if disks is None:
            return None
        out = []
        for d in disks:
            wear = d.get("wearout")
            out.append({
                "dev": d.get("devpath"),
                "model": (d.get("model") or d.get("vendor") or "").replace("_", " ").strip() or None,
                "serial": d.get("serial"),
                "size": d.get("size"),
                "type": d.get("type"),
                "rpm": d.get("rpm") if isinstance(d.get("rpm"), int) and d.get("rpm") > 0 else None,
                "health": d.get("health"),
                # Proxmox reports the SSD wear indicator as life remaining (100 = new).
                "life": wear if isinstance(wear, (int, float)) else None,
                "used": d.get("used"),
            })
        out.sort(key=lambda d: d["dev"] or "")
        return out


# --------------------------------------------------------------------------- iLO (Redfish)

def first(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


class Ilo:
    def __init__(self, cfg):
        token = base64.b64encode(f"{cfg['username']}:{cfg['password']}".encode()).decode()
        self.api = Upstream(cfg["host"], {"Authorization": "Basic " + token, "OData-Version": "4.0"},
                            verify=cfg.get("verify_ssl", False), timeout=15, label="iLO")
        self.paths = None
        self.system = None
        self.manager = None
        self.tick = 0

    def get(self, path):
        return self.api.request("GET", path)

    def member(self, collection, fallback):
        try:
            members = self.get(collection).get("Members") or []
            return members[0]["@odata.id"] if members else fallback
        except (UpstreamError, KeyError, IndexError, AttributeError):
            return fallback

    def discover(self):
        if not self.paths:
            self.paths = {
                "chassis": self.member("/redfish/v1/Chassis/", "/redfish/v1/Chassis/1/").rstrip("/") + "/",
                "system": self.member("/redfish/v1/Systems/", "/redfish/v1/Systems/1/").rstrip("/") + "/",
                "manager": self.member("/redfish/v1/Managers/", "/redfish/v1/Managers/1/").rstrip("/") + "/",
            }
        return self.paths

    def poll(self):
        p = self.discover()
        power = self.get(p["chassis"] + "Power/")
        thermal = self.get(p["chassis"] + "Thermal/")
        if self.system is None or self.tick % 3 == 0:
            self.system = self.get(p["system"])
        if self.manager is None:
            try:
                self.manager = self.get(p["manager"])
            except UpstreamError:
                self.manager = {}
        self.tick += 1
        return self.normalize(power, thermal, self.system, self.manager)

    def refresh_system(self):
        self.system = self.get(self.discover()["system"])

    @staticmethod
    def normalize(power, thermal, system, manager):
        pc = (power.get("PowerControl") or [{}])[0] or {}
        metrics = pc.get("PowerMetrics") or power.get("PowerMetrics") or {}
        psus = []
        for i, ps in enumerate(power.get("PowerSupplies") or []):
            st = ps.get("Status") or {}
            bay = (((ps.get("Oem") or {}).get("Hp") or (ps.get("Oem") or {}).get("Hpe") or {}).get("BayNumber"))
            entry = {"name": f"PSU {bay or i + 1}", "present": st.get("State") != "Absent"}
            if entry["present"]:
                entry.update({
                    "watts": ps.get("LastPowerOutputWatts"),
                    "capacity": ps.get("PowerCapacityWatts"),
                    "volts": ps.get("LineInputVoltage"),
                    "health": st.get("Health"),
                    "model": ps.get("Model"),
                })
            psus.append(entry)

        fans = []
        for i, f in enumerate(thermal.get("Fans") or []):
            st = f.get("Status") or {}
            if st.get("State") == "Absent":
                continue
            reading = first(f.get("Reading"), f.get("CurrentReading"))
            units = (f.get("ReadingUnits") or f.get("Units") or "Percent")
            fans.append({
                "name": f.get("Name") or f.get("FanName") or f"Fan {i + 1}",
                "reading": reading,
                "units": "%" if units.lower().startswith("percent") else "RPM",
                "health": st.get("Health"),
            })
        rpm_max = max([f["reading"] or 0 for f in fans if f["units"] == "RPM"] or [0]) or 1
        for f in fans:
            r = f["reading"] or 0
            f["level"] = max(0.0, min(1.0, r / 100 if f["units"] == "%" else r / max(rpm_max, 12000)))

        temps = []
        for t in thermal.get("Temperatures") or []:
            st = t.get("Status") or {}
            if st.get("State") == "Absent":
                continue
            c = first(t.get("ReadingCelsius"), t.get("CurrentReading"))
            if c is None:
                continue
            raw = t.get("Name") or "Sensor"
            temps.append({
                "name": re.sub(r"^\d+-", "", raw).strip(),
                "c": c,
                "warn": t.get("UpperThresholdCritical") or None,
                "crit": t.get("UpperThresholdFatal") or None,
                "health": st.get("Health"),
                "context": t.get("PhysicalContext"),
            })

        oem = ((system.get("Oem") or {}).get("Hp") or (system.get("Oem") or {}).get("Hpe") or {})
        bios = system.get("BiosVersion") or (((oem.get("Bios") or {}).get("Current") or {}).get("VersionString"))
        fw = manager.get("FirmwareVersion") or (((manager.get("Firmware") or {}).get("Current") or {}).get("VersionString"))
        proc = system.get("ProcessorSummary") or {}
        memsum = system.get("MemorySummary") or {}
        return {
            "watts": first(pc.get("PowerConsumedWatts"), power.get("PowerConsumedWatts")),
            "avg_watts": metrics.get("AverageConsumedWatts"),
            "min_watts": metrics.get("MinConsumedWatts"),
            "max_watts": metrics.get("MaxConsumedWatts"),
            "metrics_minutes": metrics.get("IntervalInMin"),
            "capacity_watts": pc.get("PowerCapacityWatts"),
            "psus": psus,
            "fans": fans,
            "temps": temps,
            "model": system.get("Model"),
            "serial": (system.get("SerialNumber") or "").strip() or None,
            "power_state": system.get("PowerState"),
            "health": (system.get("Status") or {}).get("Health"),
            "uid": system.get("IndicatorLED"),
            "bios": bios,
            "firmware": fw,
            "post": oem.get("PostState"),
            "cpu_summary": proc.get("Model"),
            "cpu_count": proc.get("Count"),
            "memory_gib": memsum.get("TotalSystemMemoryGiB"),
        }

    def set_uid(self, on):
        self.api.request("PATCH", self.discover()["system"], body={"IndicatorLED": "Lit" if on else "Off"})

    def power_on(self):
        target = (((self.system or {}).get("Actions") or {}).get("#ComputerSystem.Reset") or {}).get("target")
        if target:
            self.api.request("POST", target, body={"ResetType": "On"})
        else:  # older iLO 4 firmware
            self.api.request("POST", self.discover()["system"], body={"Action": "Reset", "ResetType": "On"})


# --------------------------------------------------------------------------- LAN devices

def check_device(dev):
    host, port = dev.get("host"), dev.get("port")
    start = time.monotonic()
    if port:
        try:
            with socket.create_connection((host, int(port)), timeout=1.5):
                pass
            return True, (time.monotonic() - start) * 1000
        except ConnectionRefusedError:
            return True, (time.monotonic() - start) * 1000   # it answered, so it's up
        except OSError:
            return False, None
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "1", str(host)], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            m = re.search(r"time[=<]([\d.]+)", r.stdout)
            return True, float(m.group(1)) if m else None
        return False, None
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return None, None


# --------------------------------------------------------------------------- dashboard state

class Dashboard:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.pve = Proxmox(cfg["proxmox"]) if cfg["proxmox"] else None
        self.ilo = Ilo(cfg["ilo"]) if cfg["ilo"] else None
        self.state = {"proxmox": None, "rrd": None, "disks": None, "ilo": None, "devices": []}
        self.errors = {"proxmox": None, "ilo": None}
        self.updated = {"proxmox": None, "ilo": None}
        self.history = {k: deque(maxlen=HISTORY_POINTS) for k in ("cpu", "mem", "power", "inlet")}
        self.kicks = []
        self.failed_pins = deque(maxlen=10)

    # ---- polling loops
    def start(self):
        if self.pve:
            self.spawn(self.poll_pve_fast, max(1, float(self.cfg["poll_seconds"])))
            self.spawn(self.poll_pve_slow, 60)
        if self.ilo:
            self.spawn(self.poll_ilo, 10)
        if self.cfg.get("devices"):
            self.spawn(self.poll_devices, 15)
        self.spawn(self.record_history, HISTORY_EVERY, kickable=False)

    def spawn(self, fn, every, kickable=True):
        kick = threading.Event()
        if kickable:
            self.kicks.append(kick)

        def run():
            while True:
                try:
                    fn()
                except Exception as e:  # keep the loop alive no matter what
                    log(f"{fn.__name__} crashed: {e!r}")
                kick.wait(every)
                kick.clear()

        threading.Thread(target=run, name=fn.__name__, daemon=True).start()

    def kick(self):
        for k in self.kicks:
            k.set()

    def set_error(self, source, msg):
        if self.errors[source] != msg:
            log(f"{source}: {msg}" if msg else f"{source}: connected")
        self.errors[source] = msg

    def poll_pve_fast(self):
        try:
            data = self.pve.poll_fast()
        except UpstreamError as e:
            self.set_error("proxmox", str(e))
            return
        with self.lock:
            self.state["proxmox"] = data
            self.updated["proxmox"] = time.time()
        self.set_error("proxmox", None)

    def poll_pve_slow(self):
        try:
            rrd, disks = self.pve.poll_slow()
        except UpstreamError as e:
            self.set_error("proxmox", str(e))
            return
        with self.lock:
            self.state["rrd"] = rrd
            self.state["disks"] = disks
            # Seed CPU and memory history from Proxmox so charts aren't empty on first load.
            for key, idx in (("cpu", 1), ("mem", 2)):
                if len(self.history[key]) < 3:
                    seeded = [(p[0], p[idx]) for p in rrd if p[idx] is not None]
                    self.history[key].extend(seeded[-HISTORY_POINTS:])

    def poll_ilo(self):
        try:
            data = self.ilo.poll()
        except UpstreamError as e:
            self.set_error("ilo", str(e))
            return
        with self.lock:
            self.state["ilo"] = data
            self.updated["ilo"] = time.time()
        self.set_error("ilo", None)

    def poll_devices(self):
        results = []
        for dev in self.cfg["devices"]:
            online, ms = check_device(dev)
            results.append({"name": dev.get("name") or dev.get("host"), "host": dev.get("host"),
                            "online": online, "ms": round(ms, 1) if ms is not None else None})
        with self.lock:
            self.state["devices"] = results

    def record_history(self):
        now = time.time()
        with self.lock:
            p, i = self.state["proxmox"], self.state["ilo"]
            fresh_p = self.updated["proxmox"] and now - self.updated["proxmox"] < 30
            fresh_i = self.updated["ilo"] and now - self.updated["ilo"] < 45
            if p and fresh_p:
                h = p["host"]
                if h.get("cpu") is not None:
                    self.history["cpu"].append((now, h["cpu"]))
                if h.get("mem_total"):
                    self.history["mem"].append((now, (h.get("mem_used") or 0) / h["mem_total"]))
            if i and fresh_i:
                if i.get("watts") is not None:
                    self.history["power"].append((now, i["watts"]))
                inlet = next((t for t in i.get("temps", []) if "inlet" in t["name"].lower()), None)
                if inlet:
                    self.history["inlet"].append((now, inlet["c"]))

    # ---- output
    def snapshot(self):
        with self.lock:
            st = {k: v for k, v in self.state.items()}
            hist = {k: [[round(t), round(v, 4)] for t, v in d] for k, d in self.history.items()}
            errors, updated = dict(self.errors), dict(self.updated)

        energy = None
        ilo = st["ilo"]
        if ilo:
            avg = ilo.get("avg_watts")
            if not avg and hist["power"]:
                avg = sum(v for _, v in hist["power"]) / len(hist["power"])
            if avg:
                kwh = avg * 730 / 1000  # average month
                rate = float(self.cfg.get("electricity_rate") or 0)
                energy = {"avg_watts": round(avg), "kwh_month": round(kwh),
                          "cost_month": round(kwh * rate, 2) if rate else None,
                          "currency": self.cfg.get("currency") or "$"}

        rrd = st["rrd"] or []
        net = None
        if rrd:
            last = rrd[-1]
            net = {"in": last[3], "out": last[4], "series": [[p[0], p[3], p[4]] for p in rrd]}

        title = self.cfg.get("title") or ((st["proxmox"] or {}).get("host") or {}).get("name") or (
            self.pve.node if self.pve and self.pve.node else "homelab")
        return {
            "now": time.time(),
            "title": title,
            "manage": bool(self.cfg["admin_pin"]),
            "bays": int(self.cfg.get("drive_bays") or 8),
            "temp_unit": "F" if str(self.cfg.get("temp_unit")).upper().startswith("F") else "C",
            "proxmox": {"configured": bool(self.pve), "error": errors["proxmox"],
                        "updated": updated["proxmox"], **(st["proxmox"] or {})},
            "disks": st["disks"],
            "net": net,
            "ilo": {"configured": bool(self.ilo), "error": errors["ilo"],
                    "updated": updated["ilo"], **(ilo or {})},
            "energy": energy,
            "history": hist,
            "devices": st["devices"],
        }

    # ---- management
    def check_pin(self, pin):
        now = time.time()
        recent = [t for t in self.failed_pins if now - t < 300]
        if len(recent) >= 5:
            raise PermissionError("Too many wrong PINs. Try again in a few minutes.")
        if not hmac.compare_digest(str(pin or "").encode(), self.cfg["admin_pin"].encode()):
            self.failed_pins.append(now)
            time.sleep(0.8)
            raise PermissionError("That PIN isn't right.")

    def find_guest(self, vmid, kind):
        with self.lock:
            guests = (self.state["proxmox"] or {}).get("guests") or []
        for g in guests:
            if str(g["vmid"]) == str(vmid) and g["type"] == kind:
                return g
        return None

    def act(self, req):
        if not self.cfg["admin_pin"]:
            raise PermissionError("Controls are off. Set admin_pin in config.json to turn them on.")
        self.check_pin(req.get("pin"))
        target, op = req.get("target"), req.get("op")

        if target == "guest":
            if not self.pve:
                raise ValueError("Proxmox isn't set up.")
            if op not in ("start", "shutdown", "reboot", "stop", "resume"):
                raise ValueError(f"Unknown action {op!r}.")
            g = self.find_guest(req.get("vmid"), req.get("type"))
            if not g:
                raise ValueError("That VM or container isn't on the server anymore.")
            self.pve.post(f"/nodes/{g['node']}/{g['type']}/{int(g['vmid'])}/status/{op}")
            verb = {"start": "Starting", "shutdown": "Shutting down", "reboot": "Rebooting",
                    "stop": "Force-stopping", "resume": "Resuming"}[op]
            msg = f"{verb} {g['name']}"
        elif target == "host":
            if not self.pve:
                raise ValueError("Proxmox isn't set up.")
            if op not in ("reboot", "shutdown"):
                raise ValueError(f"Unknown action {op!r}.")
            self.pve.post(f"/nodes/{self.pve.ensure_node()}/status", {"command": op})
            msg = f"{'Rebooting' if op == 'reboot' else 'Shutting down'} {self.pve.node}"
        elif target == "uid":
            if not self.ilo:
                raise ValueError("iLO isn't set up.")
            current = ((self.state["ilo"] or {}).get("uid") or "Off").lower()
            on = op == "on" or (op == "toggle" and current == "off")
            self.ilo.set_uid(on)
            self.ilo.refresh_system()
            msg = "Locator light on" if on else "Locator light off"
        elif target == "power":
            if not self.ilo or op != "on":
                raise ValueError("Power on needs iLO set up.")
            self.ilo.power_on()
            msg = "Powering on"
        else:
            raise ValueError("Unknown target.")
        log(f"action: {msg}")
        self.kick()
        return {"ok": True, "message": msg}


# --------------------------------------------------------------------------- web server

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript",
                 ".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png",
                 ".json": "application/json", ".webmanifest": "application/manifest+json",
                 ".txt": "text/plain; charset=utf-8"}


def make_handler(dash):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HomelabDash/1.0"

        def log_message(self, *args):
            pass

        def send_bytes(self, code, body, ctype, cache="no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_json(self, code, obj):
            self.send_bytes(code, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/status":
                if dash is None:
                    return self.send_json(404, {"error": "demo mode"})
                return self.send_json(200, dash.snapshot())
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    html = f.read()
                if dash is not None:
                    html = html.replace(b"<!--LIVE-->", b"<script>window.DASH_LIVE=true</script>", 1)
                return self.send_bytes(200, html, CONTENT_TYPES[".html"])
            full = os.path.realpath(os.path.join(STATIC, path.lstrip("/")))
            if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
                return self.send_bytes(404, b"Not found", "text/plain")
            ext = os.path.splitext(full)[1].lower()
            with open(full, "rb") as f:
                body = f.read()
            cache = "public, max-age=604800" if ext == ".woff2" else "no-cache"
            return self.send_bytes(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"), cache)

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/api/action" or dash is None:
                return self.send_json(404, {"error": "Not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                return self.send_json(413, {"error": "Request too large"})
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
                return self.send_json(200, dash.act(req))
            except PermissionError as e:
                return self.send_json(403, {"error": str(e)})
            except (ValueError, json.JSONDecodeError) as e:
                return self.send_json(400, {"error": str(e)})
            except UpstreamError as e:
                return self.send_json(502, {"error": str(e)})

    return Handler


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def main():
    cfg = load_config()
    dash = None
    if DEMO:
        log("Demo mode: showing simulated data, nothing is polled.")
    else:
        if not cfg["proxmox"] and not cfg["ilo"]:
            log("Neither Proxmox nor iLO is set up in config.json; the page will be empty.")
        dash = Dashboard(cfg)
        dash.start()
        log(f"Proxmox: {'on' if cfg['proxmox'] else 'not set up'}   iLO: {'on' if cfg['ilo'] else 'not set up'}   "
            f"controls: {'on' if cfg['admin_pin'] else 'off (no admin_pin)'}")
    server = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), make_handler(dash))
    server.daemon_threads = True
    log(f"Dashboard running at http://{lan_ip()}:{cfg['port']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
