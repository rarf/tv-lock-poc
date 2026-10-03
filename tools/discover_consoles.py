#!/usr/bin/env python3
"""Read-only LAN discovery for consoles (PS4/PS5/Switch/Xbox) near this host.

Discovery only: TCP connect to well-known console service ports. Nothing is
written, nothing is powered off. The goal is to learn whether the console is
reachable at all, because that decides whether software can control it.

Ports used:
  PS4  : 987  (remote play / device discovery), 9295 (websocket), 80, 443
  PS5  : 9295, 987
  Switch: none well-known for control; detected by MAC OUI only
  Xbox : 1900 (SSDP), 445, 139
"""
from __future__ import annotations

import concurrent.futures
import json
import socket
import sys

CONSOLE_PORTS = {
    "ps4": [987, 9295, 80, 443],
    "ps5": [9295, 987, 80, 443],
    "xbox": [445, 139, 80],
    "dlna": [1900],
}

# OUIs seen on consumer consoles
CONSOLE_OUIS = {
    "BC:35:1E": "Sony Interactive Entertainment (PS)",
    "00:03:93": "Sony (other)",
    "5C:EB:68": "Sony Interactive (alt)",
    "98:B6:E9": "Nintendo",
    "8C:CD:E8": "Nintendo",
    "00:1F:32": "Sony (other)",
}


def local_subnet() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return ".".join(s.getsockname()[0].split(".")[:3])
    except Exception:
        return "192.168.1"
    finally:
        s.close()


def probe(target):
    host, port = target
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect((host, port))
        return (host, port)
    except Exception:
        return None
    finally:
        s.close()


def neigh_table() -> dict:
    """Local ARP cache: ip -> mac (best effort, read-only)."""
    out = {}
    try:
        with open("/proc/net/arp") as fh:
            next(fh)
            for line in fh:
                p = line.split()
                if len(p) >= 4 and p[3] != "00:00:00:00:00:00":
                    out[p[0]] = p[3].upper()
    except Exception:
        pass
    return out


def main():
    base = local_subnet()
    arp = neigh_table()
    targets = [(f"{base}.{i}", p)
               for i in range(1, 255)
               for ports in CONSOLE_PORTS.values() for p in ports]
    found: dict[str, list[int]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=256) as ex:
        for r in ex.map(probe, targets):
            if r:
                found.setdefault(r[0], []).append(r[1])

    print(f"subnet {base}.0/24 | neigh entries: {len(arp)}")
    print("\n=== host: portas de consola abertas ===")
    for ip, ports in sorted(found.items(), key=lambda kv: int(kv[0].split(".")[-1])):
        mac = arp.get(ip, "?")
        vendor = next((v for o, v in CONSOLE_OUIS.items() if mac.startswith(o[:8])), "")
        tag = f"  <-- {vendor}" if vendor else ""
        print(f"  {ip:16} mac={mac:18} ports={sorted(ports)}{tag}")

    print("\n=== neighbours com OUI de consola (ARP) ===")
    for ip, mac in sorted(arp.items(), key=lambda kv: int(kv[0].split(".")[-1])):
        for oui, vendor in CONSOLE_OUIS.items():
            if mac.startswith(oui):
                print(f"  {ip:16} mac={mac}  {vendor}  (portas: {sorted(found.get(ip, [])) or 'nenhuma aberta'})")
                break

    json.dump({"subnet": base, "open": found, "arp": arp},
              open("/tmp/console_scan.json", "w"), indent=2)
    print("\n-> /tmp/console_scan.json")


if __name__ == "__main__":
    sys.exit(main())