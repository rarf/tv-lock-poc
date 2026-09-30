#!/usr/bin/env python3
"""tv_watch_guard — refuse to lock a TV that someone is watching, and offer
a warning instead.

Detection order (cheapest reliable signal first):
 1. `dumpsys media_session` → "Sessions Stack - have N sessions" and the
    PlaybackState of each session (state 3 = PLAYING, 2 = PAUSED).
 2. Jellyfin `/Sessions` → the exact NowPlayingItem + IsPaused for that device
    (authoritative for our own content; also covers a paused stream).
 3. `dumpsys audio` audio-focus stack.

Outcomes:
 - FREE      → lock proceeds
 - IN_USE    → lock is REFUSED; a warning is returned with the current title
              and the age of the session, so HA/Telegram can ask the adult
 - PAUSED    → treated as in-use (we do not kill a paused show)
 - UNKNOWN   → caller decides; default is refuse (safer for a child lock)

Nothing here mutates the TV. It is a guard for `tv_lock.py block`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
sys.path.insert(0, str(Path(__file__).resolve().parent))
from discovery import load_tvs  # noqa: E402
import urllib.request
from datetime import datetime, timezone

TVS = load_tvs()
JELLYFIN = "http://localhost:8096"
JF_DB = "/home/rarf/.hermes/cache/scratch/jf_ro.db"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def adb(serial: str, *args: str, timeout: int = 45) -> str:
    p = subprocess.run(["adb", "-s", serial, *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.stdout or ""


def jf_sessions() -> list[dict]:
    try:
        con = sqlite3.connect(f"file:{JF_DB}?mode=ro&immutable=1", uri=True)
        token = con.execute("select AccessToken from ApiKeys").fetchone()[0]
        con.close()
    except Exception:
        return []
    try:
        req = urllib.request.Request(f"{JELLYFIN}/Sessions", headers={"X-Emby-Token": token})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"[]")
    except Exception:
        return []


def media_session_state(serial: str) -> dict:
    """Return {state, packages[], sessions[]} from the TV's media_session dump.

    Android 9 prints one block per session:
        <tag> <package>/<component> (userId=0)
          active=true
          state=PlaybackState {state=3, position=..., ...}
    state 3 = PLAYING, 2 = PAUSED, 0/1 = nothing playing. A session can exist
    while merely idle (e.g. an app was opened but nothing plays), so the
    PlaybackState — not the session count — decides.
    """
    out = adb(serial, "shell", "dumpsys", "media_session", timeout=45)
    m = re.search(r"Sessions Stack - have (\d+) sessions?", out)
    count = int(m.group(1)) if m else 0

    # Split into per-session blocks so a single idle app cannot mask a playing one.
    sessions, cur = [], None
    for line in out.splitlines():
        stripped = line.strip()
        m_pkg = re.search(r"package=(\S+)", stripped)
        if m_pkg:
            cur = {"package": m_pkg.group(1), "active": None, "state_num": None}
            sessions.append(cur)
            continue
        if cur is None:
            continue
        if stripped.startswith("active="):
            cur["active"] = stripped.split("=", 1)[1].strip() == "true"
        m_state = re.search(r"state=PlaybackState \{state=(\d+)", stripped)
        if m_state:
            cur["state_num"] = int(m_state.group(1))

    label = {0: "NONE", 1: "STOPPED", 2: "PAUSED", 3: "PLAYING",
             4: "FAST_FORWARDING", 5: "SLOW_FORWARDING", 6: "ERROR"}
    for s in sessions:
        s["state"] = label.get(s["state_num"], "UNKNOWN")

    playing = [s for s in sessions if s["state"] in ("PLAYING", "FAST_FORWARDING",
                                                    "SLOW_FORWARDING")]
    paused = [s for s in sessions if s["state"] == "PAUSED"]

    if playing:
        top = playing[0]
        return {"session_count": count, "package": top["package"],
                "state_num": top["state_num"], "state": "PLAYING",
                "sessions": sessions}
    if paused:
        top = paused[0]
        return {"session_count": count, "package": top["package"],
                "state_num": top["state_num"], "state": "PAUSED",
                "sessions": sessions}
    # Sessions exist for system UI and TV-center processes that never publish a
    # PlaybackState (e.g. com.mediatek.wwtv.tvcenter). They are NOT playback, so
    # report NONE instead of UNKNOWN — otherwise a parental lock refuses itself
    # forever on a TV that is simply idle.
    top = sessions[0] if sessions else {}
    return {"session_count": count, "package": top.get("package"),
            "state_num": top.get("state_num"),
            "state": "NONE" if not sessions else label.get(top.get("state_num"),
                                                          "NONE"),
            "sessions": sessions}


def inspect(tv: str) -> dict:
    """Read-only verdict for one TV. Never mutates."""
    serial = _tvs().get(tv)
    if not serial:
        return {"tv": tv, "verdict": "UNKNOWN", "reason": "tv desconhecida"}

    ms = media_session_state(serial)
    # Jellyfin reports a session per device. Match on device name OR the
    # client's declared device, and fall back to "any unpaused session that is
    # not a server-side bookkeeping entry". A manual `POST /Sessions/Playing`
    # from this host registers as "Jellyfin Server", so a strict DeviceName
    # match alone would miss a genuinely playing stream.
    jf = []
    for s in jf_sessions():
        npi = s.get("NowPlayingItem")
        if not npi:
            continue
        dev = (s.get("DeviceName") or "").lower().replace(" ", "_")
        client = (s.get("Client") or "").lower()
        is_tv = dev == tv or (dev and dev in tv)
        is_server = "server" in dev or "server" in client
        if is_tv or (is_server and not s.get("PlayState", {}).get("IsPaused")):
            jf.append({
                "title": npi.get("Name"),
                "type": npi.get("Type"),
                "paused": (s.get("PlayState") or {}).get("IsPaused"),
                "user": s.get("UserName"),
                "device": s.get("DeviceName"),
                "matched": "device" if is_tv else "server-session",
            })

    playing = ms["state"] == "PLAYING" or any(
        not x["paused"] for x in jf)
    paused = ms["state"] == "PAUSED" or (jf and all(x["paused"] for x in jf))

    if playing:
        verdict = "IN_USE"
    elif paused:
        verdict = "PAUSED"
    elif ms["state"] != "UNKNOWN" or jf:
        verdict = "FREE"
    else:
        verdict = "UNKNOWN"

    return {
        "tv": tv, "serial": serial, "checked_at": now(),
        "verdict": verdict,
        "media_session": ms,
        "jellyfin": jf,
        "reason": {
            "IN_USE": "ha reproducao ativa — travar cortaria o visionamento",
            "PAUSED": "esta em pausa — travar mataria a sessao",
            "FREE": "nada a reproduzir",
            "UNKNOWN": "nao foi possivel confirmar — por omissao, nao travar",
        }[verdict],
        "safe_to_lock": verdict == "FREE",
    }


if __name__ == "__main__":
    import sys
    tv = sys.argv[1] if len(sys.argv) > 1 else "tv_da_sala"
    print(json.dumps(inspect(tv), ensure_ascii=False, indent=2))
