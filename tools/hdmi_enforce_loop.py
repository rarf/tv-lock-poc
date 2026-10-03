#!/usr/bin/env python3
"""hdmi_enforce_loop — keep the HDMI cut in place while the parental lock is on.

The one-shot lock action cuts the screen at the moment of locking, but a child
can wake the TV or switch to HDMI afterwards. This loop re-applies the cut on
a fixed interval, and only while the lock is actually engaged.

It is deliberately conservative:
  * no action at all when the lock is released (CEC stays as the adult set it)
  * no action when the TV is unreachable (it logs and waits; it does not guess)
  * it never wakes the screen
  * it exits with a non-zero status if the TV is unreachable for several cycles,
    so systemd can surface the problem instead of looping forever silently

Run:  python3 tools/hdmi_enforce_loop.py --interval 15
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hdmi_lock  # noqa: E402
from discovery import load_tvs  # noqa: E402

STATE_DIR = HERE / "state"
SERVICE = "http://127.0.0.1:8129"
TOKEN_FILE = STATE_DIR / "api_token"

MAX_CONSECUTIVE_FAILURES = 5


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token() -> str:
    return TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""


def service_get(path: str) -> dict | None:
    import urllib.request
    req = urllib.request.Request(SERVICE + path,
                                 headers={"X-TV-Lock-Token": token()})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001
        print(f"[{now()}] servico inacessivel: {type(e).__name__}: {e}", flush=True)
        return None


def is_locked(tv: str) -> tuple[bool | None, dict]:
    st = service_get(f"/status?tv={tv}")
    if st is None:
        return None, {}
    rec = st.get("tvs", {}).get(tv, {})
    # enabled=True means the lock service considers this TV locked
    return bool(rec.get("enabled")), rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv", default=None)
    ap.add_argument("--interval", type=int, default=15)
    a = ap.parse_args()

    tvs = list(load_tvs().keys())
    tv = a.tv or (tvs[0] if tvs else "tv_sala")
    if not token():
        print("FALTA tools/state/api_token", file=sys.stderr)
        return 2

    print(f"hdmi_enforce: tv={tv} intervalo={a.interval}s", flush=True)
    fails = 0
    last_report = ""
    while True:
        locked, rec = is_locked(tv)
        if locked is None:
            fails += 1
            if fails >= MAX_CONSECUTIVE_FAILURES:
                print(f"[{now()}] {fails} ciclos sem servico — a sair", flush=True)
                return 1
        else:
            fails = 0
            if locked:
                r = hdmi_lock.enforce(tv)
                if r.get("action") not in ("nada_a_fazer", "nao travado"):
                    print(f"[{now()}] {r}", flush=True)
                    last_report = r.get("action", "")
            elif last_report:
                print(f"[{now()}] destravada — HDMI devolvido", flush=True)
                last_report = ""
        time.sleep(max(5, a.interval))


if __name__ == "__main__":
    sys.exit(main())