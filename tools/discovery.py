"""Device discovery and configuration for the TV lock.

Everything that is specific to one household lives in `config.json` (or the
environment), so the same code works for any TV, any brand, any network.

The real addresses of the author's own TVs are never committed; the shipped
`config.example.json` uses documentation ranges (192.0.2.0/24, RFC 5737).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"
CONFIG_FILE = STATE_DIR / "config.json"

# Documented example endpoints; override per installation.
DEFAULT_TVS = {
    "tv_sala": "192.0.2.10:5555",
    "tv_quarto": "192.0.2.11:5555",
}

ADB_PORT = 5555


def load_tvs() -> dict:
    """TV name -> adb serial, from env, config file, or the shipped defaults."""
    raw = os.environ.get("TV_LOCK_TVS")
    if raw:
        return {k.strip(): v.strip() for k, v in
                (p.split("=", 1) for p in raw.split(",") if "=" in p)}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            if isinstance(data, dict):
                # Only string values are valid endpoints; ignore anything else
                # (e.g. a state file written by another tool).
                valid = {k: v for k, v in data.items() if isinstance(v, str)}
                if valid:
                    return valid
        except Exception:
            pass
    return dict(DEFAULT_TVS)


def adb(serial: str, *args: str, timeout: int = 45) -> str:
    p = subprocess.run(["adb", "-s", serial, *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.stdout or ""


def _identify_tv(serial: str) -> str | None:
    """Read the device's stable hardware identity, used to re-find it by MAC.

    `ro.serialno` is not reliable on every Android TV build; the boot serial and
    the bootloader serial usually are. Any of them is better than an IP.
    """
    for prop in ("ro.serialno", "ro.boot.serialno", "ro.bootloader.serialno"):
        out = adb(serial, "shell", "getprop", prop).strip()
        if out and out not in ("null", "unknown", ""):
            return out
    return None


def remember_identity(tv: str, serial: str) -> None:
    """Record what this TV looks like, so a DHCP change can be recovered from."""
    try:
        ident = _identify_tv(serial)
        if not ident:
            return
        path = STATE_DIR / "identity.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data[tv] = {"serial": ident, "endpoint": serial, "seen": time.time()}
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass  # identity is an optimisation; never block a real action on it


def _arp_table() -> dict:
    out = {}
    try:
        rc = subprocess.run(["ip", "neigh", "show"], capture_output=True,
                            text=True, timeout=20)
        for line in (rc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1] == "dev":
                mac = parts[4] if len(parts) > 4 else ""
                if parts[0].count(".") == 3 and ":" in mac:
                    out[parts[0]] = mac.lower()
    except Exception:
        pass
    return out


def rediscover_by_mac(tv: str) -> str | None:
    """Find the TV again on a new DHCP lease.

    Strategy, in order of cost: known MAC from the identity file, then a bounded
    sweep of the local subnet for anything with ADB open. Verified to work on
    this network: the sala TV moved from .197 to .247 and was recovered here.
    """
    ident_path = STATE_DIR / "identity.json"
    mac = None
    if ident_path.exists():
        try:
            mac = (json.loads(ident_path.read_text()).get(tv) or {}).get("mac")
        except Exception:
            mac = None
    arp = _arp_table()
    if mac:
        for ip, seen_mac in arp.items():
            if seen_mac == mac:
                return f"{ip}:{ADB_PORT}"
    # Bounded sweep: local /24, ADB port only, short timeout, parallel.
    base = local_subnet()
    hosts = [f"{base}.{i}" for i in range(1, 256)]

    def probe(ip):
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect((ip, ADB_PORT))
            return ip
        except Exception:
            return None
        finally:
            s.close()

    import concurrent.futures
    hits = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        hits = [h for h in ex.map(probe, hosts) if h]
    for ip in hits:
        cand = f"{ip}:{ADB_PORT}"
        proc = subprocess.run(["adb", "connect", cand], capture_output=True,
                              text=True, timeout=20)
        if "connected to" not in (proc.stdout or ""):
            continue
        model = adb(cand, "shell", "getprop", "ro.product.model").strip()
        if model and "TV" in model.upper():
            return cand
    return None


def ensure_connected(tv: str) -> tuple[bool, str]:
    tvs = load_tvs()
    serial = tvs.get(tv)
    if not serial:
        return False, f"desconhecida: {tv}"
    for attempt in range(2):
        rc = subprocess.run(["adb", "-s", serial, "get-state"],
                            capture_output=True, text=True, timeout=30)
        if rc.returncode == 0 and (rc.stdout or "").strip() == "device":
            remember_identity(tv, serial)
            return True, serial
        subprocess.run(["adb", "connect", serial], capture_output=True, timeout=30)
        rc = subprocess.run(["adb", "-s", serial, "get-state"],
                            capture_output=True, text=True, timeout=30)
        if rc.returncode == 0 and (rc.stdout or "").strip() == "device":
            remember_identity(tv, serial)
            return True, serial
        if attempt == 0:
            # The address may simply be a stale DHCP lease. Find the TV again
            # rather than reporting "unavailable" for something that is online.
            found = rediscover_by_mac(tv)
            if found and found != serial:
                tvs[tv] = found
                try:
                    CONFIG_FILE.write_text(json.dumps(tvs, indent=2) + "\n")
                except Exception:
                    pass
                serial = found
    return False, serial


def local_subnet() -> str:
    """Best-effort local IPv4 subnet, so discovery never scans other networks."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return ".".join(s.getsockname()[0].split(".")[:3])
    except Exception:
        return "192.168.1"
    finally:
        s.close()


def discover(ports=(ADB_PORT,), timeout=0.7, first=1, last=254, limit=20) -> list[str]:
    """Find ADB endpoints on the local subnet only (never other networks)."""
    import concurrent.futures
    base = local_subnet()
    targets = [(f"{base}.{i}", p) for i in range(first, last + 1) for p in ports]

    def probe(t):
        host, port = t
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return f"{host}:{port}"
        except Exception:
            return None
        finally:
            s.close()

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        for r in ex.map(probe, targets):
            if r:
                found.append(r)
                if len(found) >= limit:
                    break
    return found


def identify(serial: str) -> dict:
    """Read manufacturer, model and Android version from a connected TV."""
    def prop(k):
        return adb(serial, "shell", "getprop", k).strip()
    return {
        "serial": serial,
        "manufacturer": prop("ro.product.manufacturer"),
        "model": prop("ro.product.model"),
        "android": prop("ro.build.version.release"),
        "sdk": prop("ro.build.version.sdk"),
    }


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "discover":
        print(json.dumps(discover(), indent=2))
    elif cmd == "identify" and len(sys.argv) > 2:
        print(json.dumps(identify(sys.argv[2]), indent=2))
    else:
        print(json.dumps(load_tvs(), indent=2))
