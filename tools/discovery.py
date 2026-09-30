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


def ensure_connected(tv: str) -> tuple[bool, str]:
    tvs = load_tvs()
    serial = tvs.get(tv)
    if not serial:
        return False, f"desconhecida: {tv}"
    rc = subprocess.run(["adb", "-s", serial, "get-state"],
                        capture_output=True, text=True, timeout=30)
    if rc.returncode == 0 and (rc.stdout or "").strip() == "device":
        return True, serial
    subprocess.run(["adb", "connect", serial], capture_output=True, timeout=30)
    rc = subprocess.run(["adb", "-s", serial, "get-state"],
                        capture_output=True, text=True, timeout=30)
    return rc.returncode == 0 and (rc.stdout or "").strip() == "device", serial


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
