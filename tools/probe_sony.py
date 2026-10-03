#!/usr/bin/env python3
"""Identify and probe Sony consoles on the LAN. Strictly read-only.

Nothing here powers a console on or off. It only opens TCP connections and
reads the discovery/wakeup banners, so we can prove whether software control
is possible at all before designing the lock.

PS4 discovery (UDP 987) replies with an HTTP-ish banner that names the device
and whether it is powered on. That single packet answers both questions.
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import time

CANDIDATES = ["192.168.1.128", "192.168.1.135", "192.168.1.136"]

# PS4 "SRCH" discovery request, as used by the official Remote Play app.
SRCH = b"\x00\x00\x00\x00SRCH * HTTP/1.1\n"
SRCH += b"device-discovery-name: PS4\n"
SRCH += b"device-discovery-app-type: ps4\n\n"


def ps4_probe(ip: str, timeout: float = 2.5) -> dict:
    """Send the PS4 discovery packet and read the reply."""
    out = {"ip": ip}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(SRCH, (ip, 987))
        data, _ = s.recvfrom(2048)
        out["banner"] = data.decode("utf-8", "replace")[:600]
        out["responded"] = True
        # banner looks like: HTTP/1.1 200 OK\n\nHTTP/1.1 200 OK\nmodel-name:...; ...
        low = out["banner"].lower()
        out["model"] = "PS4"
        if "ps5" in low:
            out["model"] = "PS5"
        out["powered_on"] = ("standby" not in low) and ("http/1.1 200" in low)
    except socket.timeout:
        out["responded"] = False
        out["banner"] = "(sem resposta UDP 987 em %.1fs)" % timeout
    except Exception as e:
        out["responded"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        s.close()
    return out


def tcp_probe(ip: str, ports=(987, 9295)) -> dict:
    res = {}
    for p in ports:
        s = socket.socket()
        s.settimeout(1.2)
        try:
            s.connect((ip, p))
            res[str(p)] = "aberta"
        except Exception as e:
            res[str(p)] = type(e).__name__
        finally:
            s.close()
    return res


def main():
    report = []
    for ip in CANDIDATES:
        print(f"===== {ip} =====")
        d = ps4_probe(ip)
        print(f"  UDP 987 respondeu: {d['responded']}")
        print(f"  banner: {d.get('banner','')[:400]}")
        t = tcp_probe(ip)
        print(f"  TCP: {t}")
        d["tcp"] = t
        report.append(d)
        print()
    json.dump(report, open("/tmp/sony_probe.json", "w"), indent=2, ensure_ascii=False)
    print("-> /tmp/sony_probe.json")


if __name__ == "__main__":
    main()