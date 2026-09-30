#!/usr/bin/env python3
"""tv_hdmi_guard — when the TV is locked, a switched-to-HDMI input gets the
screen turned off, and staying on HDMI keeps it off.

Design constraints discovered on the MiTV-MSSP3 (Android 9, no root):

 * `KEYCODE_SLEEP` puts the panel into `mWakefulness=Asleep` and
   `KEYCODE_WAKEUP` brings it back to `Awake`. Both verified by readback.
 * Wake-on-LAN does NOT work: this panel's "standby" is screen-off, not
   power-off, so the device keeps answering ADB and never listens for a magic
   packet. A TV parked this way can still be woken with the physical remote or
   the power button — deliberately, because a child must not be able to undo
   it with a button on a controller.
 * The current source is readable from `dumpsys window`:
     mCurrentFocus=Window{... u0 com.google.android.tvlauncher/...}  -> Android
     mCurrentFocus=Window{... u0 Sys2023:dream}                      -> HDMI/AV UI
   `mHdmiPlugged` only reports whether a cable is physically present, not which
   source is selected, so it cannot drive the decision.
 * `KEYCODE_TV_INPUT` exists and is bound to Xiaomi's source switcher, so the
   source can also be pushed back to Android with an intent.

Behaviour:
   FREE  -> nothing happens.
   LOCKED + source is HDMI -> issue KEYCODE_SLEEP, remember the TV as
   "parked by guard" so the sensor exposes it, and re-issue it if something
   wakes the screen while the lock is on.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from discovery import load_tvs  # noqa: E402
sys.path.insert(0, str(HERE))

STATE = HERE / "state" / "hdmi_guard.json"
TVS = load_tvs()

# Windows that mean the panel is showing the external source.
HDMI_FOCUS = re.compile(
    r"mCurrentFocus=Window\{[^}]*?\s(?:u0\s+)?(Sys2023:dream|"
    r"[^\s/}]*hdmi[^\s/}]*|[^\s/}]*HDMI[^\s/}]*)", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def adb(serial: str, *args: str, timeout: int = 45) -> str:
    p = subprocess.run(["adb", "-s", serial, *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.stdout or ""


def load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            return {}
    return {}


def save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2))


def is_asleep(serial: str) -> bool:
    out = adb(serial, "shell", "dumpsys", "power", timeout=40)
    m = re.search(r"mWakefulness=(\w+)", out)
    return (m.group(1) if m else "?") in ("Asleep", "Dreaming")


def current_source(serial: str) -> str:
    out = adb(serial, "shell", "dumpsys", "window", timeout=40)
    m = re.search(r"mCurrentFocus=Window\{[^}]*?\s(?:u0\s+)?([^/}\s]+)", out)
    focus = m.group(1) if m else "?"
    if HDMI_FOCUS.search("mCurrentFocus=" + focus) or "Sys2023" in focus:
        return "hdmi"
    if "tvlauncher" in focus or "tvhome" in focus or "google.android.tv" in focus:
        return "android"
    if "nada" in focus or "StatusBar" in focus or "Keyguard" in focus:
        return "home"
    return focus


def sleep_panel(serial: str) -> None:
    adb(serial, "shell", "input", "keyevent", "KEYCODE_SLEEP", timeout=30)


def force_android(serial: str) -> bool:
    """Best-effort: push the source back to Android via the TV input intent."""
    intents = [
        ("shell", "am", "start", "-a", "android.intent.action.VIEW"),
        ("shell", "input", "keyevent", "KEYCODE_HOME"),
    ]
    ok = False
    for args in intents:
        out = adb(serial, *args, timeout=30)
        if "Error" not in out and "Exception" not in out:
            ok = True
            time.sleep(1)
    return ok


def enforce(tv: str, locked: bool) -> dict:
    serial = _tvs().get(tv)
    if not serial:
        return {"ok": False, "error": f"tv desconhecida: {tv}"}
    subprocess.run(["adb", "-s", serial, "connect"], capture_output=True, timeout=30)
    state = load()
    rec = state.setdefault(tv, {"parked": False, "last_action": None, "at": None})

    before = {"source": current_source(serial), "asleep": is_asleep(serial)}
    if not locked:
        rec.update({"parked": False, "last_action": None, "at": now(),
                    "last_seen_source": before["source"]})
        save(state)
        return {"ok": True, "action": "none", "locked": False, "observed": before}

    acted = None
    if before["source"] == "hdmi":
        if not before["asleep"]:
            sleep_panel(serial)
            acted = "slept_screen"
        rec["parked"] = True
    elif rec.get("parked") and not before["asleep"]:
        # Woken up while the lock is on: put it back to sleep.
        sleep_panel(serial)
        acted = "re_slept_after_wake"

    after = {"source": current_source(serial), "asleep": is_asleep(serial)}
    rec.update({"last_action": acted, "at": now(), "last_seen_source": before["source"]})
    save(state)
    return {"ok": True, "action": acted or "none", "locked": True,
            "observed_before": before, "observed_after": after,
            "parked": rec["parked"]}


def status(tv: str) -> dict:
    serial = _tvs().get(tv)
    if not serial:
        return {"ok": False, "error": "tv desconhecida"}
    rec = load().get(tv, {})
    return {"ok": True, "tv": tv, "serial": serial,
            "source": current_source(serial), "asleep": is_asleep(serial),
            "parked_by_guard": rec.get("parked", False),
            "last_action": rec.get("last_action"),
            "at": rec.get("at"),
            "wol_supported": False}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv", default="tv_da_sala")
    ap.add_argument("--locked", action="store_true",
                    help="aplica a regra de ecrã quando a TV está travada")
    a = ap.parse_args()
    print(json.dumps(enforce(a.tv, a.locked) if a.locked else status(a.tv),
                     ensure_ascii=False, indent=2))
