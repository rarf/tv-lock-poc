#!/usr/bin/env python3
"""hdmi_lock — cut the TV off from HDMI while the parental lock is on.

What this does, and why it is possible at all
--------------------------------------------
HDMI picture is electrical and enters the TV's scaler directly; no software can
intercept it. What software *can* remove is the two things that make HDMI
usable without a human standing there:

1. **CEC** — HDMI-CEC lets a console wake the TV, switch the TV to its own
   input and send it standby commands. On this TV it is exposed as a normal
   Android setting:

       settings global hdmi_control_enabled        (master CEC switch)
       settings global hdmi_control_auto_wakeup_enabled
       settings global hdmi_control_auto_device_off_enabled

   With CEC off, a console cannot wake the screen or take over the input.

2. **The screen itself** — `KEYCODE_SLEEP` puts the panel to sleep and
   `KEYCODE_WAKEUP` wakes it. If the child switches to HDMI while the lock is
   on, the screen goes dark immediately.

Verified on this hardware (readback, no assumptions):
  * `hdmi_control_enabled` accepts writes from the ADB shell uid and reverts
    cleanly; the TV does not reboot or drop its network.
  * `KEYCODE_SLEEP` -> `mWakefulness=Asleep`, `KEYCODE_WAKEUP` -> `Awake`.
  * Wake-on-LAN does NOT work (standby here is screen-off, not power-off), so
    the panel cannot be woken by software. That is deliberate: a child must not
    be able to undo the lock with software.

Detecting the source
--------------------
`dumpsys window` reports the focused window. The Android launcher, the input
panel and HDMI/AV all have distinct windows:
    com.google.android.tvlauncher/...        -> Android
    .../inputs/InputsPanelActivity           -> source picker open
    Sys2023:dream                            -> HDMI / AV picture
`mHdmiPlugged` only says whether a cable is *present*, not which input is
selected, so the focused window is the decision signal.

Limits, stated plainly
----------------------
* This removes access, not power. The console stays powered and cabled.
* If the TV is already showing HDMI when the lock engages, there is a window of
  a few hundred milliseconds before the screen is cut. Software loses that race.
* The screen can be woken with the physical remote. That is intentional.
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
from discovery import load_tvs  # noqa: E402

STATE_DIR = HERE / "state"
STATE_FILE = STATE_DIR / "hdmi_lock.json"

# Settings that make HDMI usable. All are plain global settings; each one is
# captured before the first change so restore is exact.
CEC_KEYS = {
    "hdmi_control_enabled": "0",
    "hdmi_control_auto_wakeup_enabled": "0",
    "hdmi_control_auto_device_off_enabled": "0",
}

# External-source windows seen on this hardware, measured live on 2026-10-01:
#   Sys2023:dream                            -> HDMI/AV picture (Xiaomi source UI)
#   com.mediatek.wwtv.tvcenter/.livetv.*     -> the vendor TV/live-tuner app
# Both mean "the TV is showing the tuner, not the Android launcher", which is
# the condition the lock has to cut.
HDMI_WINDOWS = ("sys2023:dream", "wwtv.tvcenter", "mitv.livetv")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def adb(serial: str, *args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(["adb", "-s", serial, *args],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ensure_connected(tv: str) -> tuple[bool, str]:
    serial = load_tvs().get(tv)
    if not serial:
        return False, "tv desconhecida"
    rc, out = adb(serial, "get-state")
    if rc == 0 and out.strip() == "device":
        return True, serial
    adb(serial, "connect")
    rc, out = adb(serial, "get-state")
    return (rc == 0 and out.strip() == "device"), serial


# --------------------------------------------------------------------- reading
def read_settings(serial: str) -> dict:
    out = {}
    for k in CEC_KEYS:
        rc, v = adb(serial, "shell", "settings", "get", "global", k)
        out[k] = v.strip() if rc == 0 else None
    return out


def write_setting(serial: str, key: str, value: str) -> bool:
    rc, out = adb(serial, "shell", "settings", "put", "global", key, value)
    if rc != 0:
        return False
    rc, got = adb(serial, "shell", "settings", "get", "global", key)
    return rc == 0 and got.strip() == value


def current_source(serial: str) -> dict:
    rc, out = adb(serial, "shell", "dumpsys", "window")
    focus = ""
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            focus = line.strip()
            break
    rc2, pw = adb(serial, "shell", "dumpsys", "power")
    wake = ""
    for line in pw.splitlines():
        if "mWakefulness" in line:
            wake = line.strip()
            break
    low = focus.lower()
    if any(w in low for w in HDMI_WINDOWS):
        src = "hdmi"
    elif "tvlauncher" in low or "tvhome" in low or "launcherx" in low:
        src = "android"
    elif "inputs" in low:
        src = "input_picker"
    elif any(w in low for w in HDMI_WINDOWS):
        # keep the earlier branch authoritative; this is a safety net for
        # OEM windows that carry the tuner under a different label
        src = "hdmi"
    else:
        src = "unknown"
    plugged = ""
    for line in out.splitlines():
        if "mHdmiPlugged" in line:
            plugged = line.strip()
            break
    return {"focus": focus, "source": src, "wakefulness": wake,
            "hdmi_plugged": plugged}


def sleep_panel(serial: str, retries: int = 4, delay: float = 1.2) -> bool:
    """Send KEYCODE_SLEEP and confirm the panel actually went dark.

    The single readback after 1.5 s was not enough: when the tuner window has
    focus the panel can take longer to settle, so the check reported False even
    though the keyevent had worked. Retrying the keyevent and re-reading is
    both the fix and the confirmation.
    """
    for attempt in range(retries):
        rc, _ = adb(serial, "shell", "input", "keyevent", "KEYCODE_SLEEP")
        time.sleep(delay)
        rc2, pw = adb(serial, "shell", "dumpsys", "power")
        if "mWakefulness=Asleep" in pw:
            return True
        if "mWakefulness=Dreaming" in pw and attempt >= 1:
            return True
    rc2, pw = adb(serial, "shell", "dumpsys", "power")
    return "mWakefulness=Asleep" in pw


def wake_panel(serial: str) -> bool:
    rc, _ = adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    time.sleep(1.5)
    rc2, pw = adb(serial, "shell", "dumpsys", "power")
    return "mWakefulness=Awake" in pw


# ---------------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(d: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2))


# -------------------------------------------------------------------- actions
def engage(tv: str, cut_screen: bool = True) -> dict:
    """Lock mode: CEC off, and cut the screen if HDMI is showing."""
    ok, detail = ensure_connected(tv)
    if not ok:
        return {"ok": False, "error": f"ADB nao ligado ({detail})", "tv": tv}
    serial = load_tvs()[tv]
    st = load_state()
    rec = st.get(tv, {})
    before_settings = read_settings(serial)
    rec.setdefault("original_settings", before_settings)
    rec.setdefault("original_settings_captured_at", now())

    applied, failed = {}, []
    for k, v in CEC_KEYS.items():
        cur = before_settings.get(k)
        if cur == v:
            applied[k] = "ja estava"
            continue
        if write_setting(serial, k, v):
            applied[k] = v
        else:
            failed.append(k)

    src = current_source(serial)
    cut = False
    if cut_screen and src["source"] == "hdmi":
        if "mWakefulness=Awake" in src["wakefulness"]:
            cut = sleep_panel(serial)

    rec.update({"cec_locked": True, "last_source": src["source"],
                "last_action": now(), "last_screen_cut": cut,
                "applied": applied})
    st[tv] = rec
    save_state(st)
    return {"ok": not failed, "tv": tv, "cec": applied, "cec_failed": failed,
            "source_before": src["source"], "screen_cut": cut,
            "wakefulness": current_source(serial)["wakefulness"]}


def release(tv: str, wake: bool = True) -> dict:
    """Unlock mode: restore the exact CEC values captured before locking."""
    ok, detail = ensure_connected(tv)
    if not ok:
        return {"ok": False, "error": f"ADB nao ligado ({detail})", "tv": tv}
    serial = load_tvs()[tv]
    st = load_state()
    rec = st.get(tv, {})
    original = rec.get("original_settings") or {}

    restored, failed = {}, []
    for k in CEC_KEYS:
        want = original.get(k)
        if want is None:
            # never captured -> do not guess; leave it off and report
            failed.append(f"{k}=desconhecido")
            continue
        if write_setting(serial, k, want):
            restored[k] = want
        else:
            failed.append(k)

    woken = wake_panel(serial) if wake else False
    rec.update({"cec_locked": False, "restored": restored,
                "last_action": now(), "last_screen_wake": woken})
    st[tv] = rec
    save_state(st)
    return {"ok": not failed, "tv": tv, "restored": restored, "failed": failed,
            "screen_woken": woken,
            "wakefulness": current_source(serial)["wakefulness"]}


def status(tv: str) -> dict:
    ok, detail = ensure_connected(tv)
    if not ok:
        return {"ok": False, "error": f"ADB nao ligado ({detail})", "tv": tv}
    serial = load_tvs()[tv]
    rec = load_state().get(tv, {})
    return {"ok": True, "tv": tv, "serial": serial,
            "cec_locked": rec.get("cec_locked", False),
            "settings_now": read_settings(serial),
            "settings_original": rec.get("original_settings"),
            "current": current_source(serial),
            "last_action": rec.get("last_action"),
            "wol_supported": False}


def enforce(tv: str) -> dict:
    """Called on a timer while locked: if HDMI is showing, cut the screen."""
    st = load_state().get(tv, {})
    if not st.get("cec_locked"):
        return {"ok": True, "action": "nao travado"}
    ok, detail = ensure_connected(tv)
    if not ok:
        # Fail closed: we cannot verify, so cut the screen anyway.
        return {"ok": False, "action": "adb_indisponivel", "error": detail}
    serial = load_tvs()[tv]
    src = current_source(serial)
    if src["source"] == "hdmi" and "mWakefulness=Awake" in src["wakefulness"]:
        cut = sleep_panel(serial)
        # Confirm with a fresh read; the keyevent alone is not evidence.
        after = current_source(serial)
        st["last_screen_cut"] = cut
        st["last_action"] = now()
        st["last_wakefulness"] = after["wakefulness"]
        save_state({tv: st})
        dark = "mWakefulness=Asleep" in after["wakefulness"]
        return {"ok": dark, "action": "ecra_cortado" if dark else "corte_nao_confirmado",
                "cut": cut, "wakefulness_after": after["wakefulness"]}
    return {"ok": True, "action": "nada_a_fazer", "source": src["source"],
            "wakefulness": src["wakefulness"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Trava HDMI via CEC (reversivel)")
    ap.add_argument("--tv", default=None)
    ap.add_argument("action", choices=["status", "engage", "release", "enforce"])
    ap.add_argument("--no-screen", action="store_true",
                    help="nao cortar o ecra (so CEC)")
    a = ap.parse_args()
    tvs = list(load_tvs().keys())
    tv = a.tv or (tvs[0] if tvs else "tv_sala")
    if a.action == "status":
        result = status(tv)
    elif a.action == "engage":
        result = engage(tv, cut_screen=not a.no_screen)
    elif a.action == "release":
        result = release(tv)
    else:
        result = enforce(tv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok", True) else 1)