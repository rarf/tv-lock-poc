#!/usr/bin/env python3
"""Estado ao vivo da TV da sala (Android TV / Mi Box) + Jellyfin.

Fontes, por ordem de confiança:
  1. Jellyfin /Sessions  -> titulo exato quando quem toca e o Jellyfin
  2. ADB (<TV_IP>:5555) -> ecra ligado/desligado, app em primeiro plano,
     estado de reproducao (playing/paused) e posicao
  3. uiautomator (--deep) -> texto no ecra, quando a app nao esta num video
     protegido por DRM (Netflix/Prime bloqueiam screenshot e metadados)

Uso:
  python3 tv_sala.py                 # linha legivel + JSON
  python3 tv_sala.py --json          # so JSON
  python3 tv_sala.py --state-file S  # compara com o estado anterior em S
  python3 tv_sala.py --deep          # inclui texto do ecra (mais lento)

Somente leitura: nunca altera nada na TV nem no Jellyfin.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request

TV_HOST = os.environ.get("TV_SALA_ADB", "<TV_IP>:5555")
TV_DEVICE_NAME = "TV da sala"
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
JF_CONTAINER = os.environ.get("JELLYFIN_CONTAINER", "http://localhost:8096")
JF_DB_IN_CONTAINER = os.environ.get("JELLYFIN_DB", "/config/data/jellyfin.db")

APP_NAMES = {
    "com.netflix.ninja": "Netflix",
    "org.jellyfin.androidtv": "Jellyfin",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvunplugged": "YouTube TV",
    "com.disney.disneyplus": "Disney+",
    "com.wbd.stream": "Max",
    "com.hbo.hbonow": "HBO",
    "com.globo.globoplay": "Globoplay",
    "com.spotify.tv.android": "Spotify",
    "com.google.android.tvlauncher": "(launcher da TV)",
    "com.google.android.apps.tv.launcherx": "(launcher da TV)",
    "com.android.systemui": "(sistema)",
    "com.google.android.katniss": "Google Assistente",
    "com.google.android.videos": "Google TV",
    "com.plexapp.android": "Plex",
    "org.videolan.vlc": "VLC",
    "com.casa.dispatcharr": "Dispatcharr",
}

PLAYBACK_STATES = {
    0: "parado",
    1: "parado",
    2: "em pausa",
    3: "a tocar",
    4: "a avancar",
    5: "a saltar",
    6: "a carregar",
    7: "erro",
    8: "a carregar",
    9: "a carregar",
    10: "a carregar",
    11: "a carregar",
}

# Aplicacoes cujo video protegido impede screenshot/metadados (DRM).
DRM_APPS = {
    "com.netflix.ninja",
    "com.amazon.amazonvideo.livingroom",
    "com.disney.disneyplus",
    "com.wbd.stream",
    "com.hbo.hbonow",
    "com.globo.globoplay",
}


def run(cmd, timeout=20):
    """Corre um comando e devolve (rc, stdout)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except FileNotFoundError as exc:
        return 127, str(exc)


def adb(*args, timeout=20):
    return run(["adb", "-s", TV_HOST, *args], timeout=timeout)


def adb_ensure_connected():
    rc, out = run(["adb", "devices"], timeout=15)
    if TV_HOST in out and "device" in out:
        return True, "ja ligado"
    rc, out = run(["adb", "connect", TV_HOST], timeout=20)
    if "connected" not in out:
        return False, out.strip()
    rc, out = run(["adb", "devices"], timeout=15)
    if f"{TV_HOST}\tdevice" in out:
        return True, "ligado agora"
    return False, out.strip()


def jellyfin_token():
    """Token de API valido: env var, ou copia da jellyfin.db do container."""
    tok = os.environ.get("JELLYFIN_API_KEY")
    if tok:
        return tok
    tmpdir = tempfile.mkdtemp(prefix="tvsala-")
    dst = os.path.join(tmpdir, "jellyfin.db")
    try:
        rc, out = run(["docker", "cp", f"{JF_CONTAINER}:{JF_DB_IN_CONTAINER}", dst], timeout=90)
        if rc != 0 or not os.path.exists(dst):
            return None
        con = sqlite3.connect(dst)
        row = con.execute("select AccessToken from ApiKeys limit 1").fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def jellyfin_sessions():
    """Sessoes ativas no Jellyfin; devolve lista de dicts curtos."""
    tok = jellyfin_token()
    if not tok:
        return None, "sem token de API do Jellyfin"
    req = urllib.request.Request(
        f"{JELLYFIN_URL}/Sessions",
        headers={"X-Emby-Token": tok, "Accept": "application/json"},
    )
    try:
        data = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as exc:
        return None, f"Jellyfin /Sessions falhou: {exc}"
    out = []
    for s in data:
        st = s.get("PlayState") or {}
        item = s.get("NowPlayingItem") or {}
        out.append(
            {
                "device": s.get("DeviceName"),
                "client": s.get("Client"),
                "user": s.get("UserName"),
                "playing": bool(item),
                "title": item.get("Name"),
                "series": item.get("SeriesName"),
                "episode": item.get("IndexNumber"),
                "channel": item.get("ChannelName"),
                "type": item.get("Type"),
                "position_s": round((st.get("PositionTicks") or 0) / 10_000_000) or None,
                "paused": st.get("IsPaused"),
                "play_method": st.get("PlayMethod"),
                "transcoding": bool(s.get("TranscodingInfo")),
                "last_activity": s.get("LastActivityDate"),
            }
        )
    return out, None


def parse_media_session(text):
    """Extrai o PlaybackState ativo (state/position) e o pacote."""
    state = position = None
    package = None
    for line in text.splitlines():
        line = line.strip()
        if "packages=" in line:
            package = line.split("packages=")[-1].strip()
        if line.startswith("state=PlaybackState"):
            body = line.split("PlaybackState", 1)[1]
            for part in body.strip("{} ").split(","):
                part = part.strip()
                if part.startswith("state="):
                    try:
                        state = int(part.split("=")[1])
                    except ValueError:
                        pass
                elif part.startswith("position="):
                    try:
                        position = int(part.split("=")[1])
                    except ValueError:
                        pass
    return state, position, package


def read_tv(deep=False):
    ok, msg = adb_ensure_connected()
    res = {"reachable": ok, "connection": msg}
    if not ok:
        return res

    rc, power = adb("shell", "dumpsys power")
    wake = None
    for line in power.splitlines():
        if "mWakefulness=" in line:
            wake = line.split("mWakefulness=")[1].split()[0].strip()
            break
    screen_on = "state=ON" in power.split("Display Power:")[-1][:40] if "Display Power:" in power else None
    # Awake = ecra ligado a uso; Dreaming = screensaver ativo; Asleep/Dozing = standby
    state_label = {"Awake": "ligada", "Dreaming": "screensaver", "Asleep": "standby", "Dozing": "standby"}.get(wake, wake)
    res["wakefulness"] = wake
    res["screen_state"] = state_label
    res["awake"] = wake == "Awake"
    res["screen_on"] = screen_on
    res["tv_on"] = bool(wake == "Awake" and screen_on)

    rc, win = adb("shell", "dumpsys window")
    pkg = None
    for line in win.splitlines():
        if "mCurrentFocus=" in line or "mFocusedApp=" in line:
            chunk = line.split("u0 ", 1)[-1]
            cand = chunk.split("/")[0].strip()
            if cand.startswith("com.") or cand.startswith("org."):
                pkg = cand
                break
    res["package"] = pkg
    res["app"] = APP_NAMES.get(pkg, pkg)

    rc, ms = adb("shell", "dumpsys media_session")
    state, position, ms_pkg = parse_media_session(ms)
    playing_state = None
    if state is not None:
        playing_state = PLAYBACK_STATES.get(state, f"codigo {state}")
    res["playback_state"] = playing_state
    res["playback_position_s"] = round(position / 1000) if position is not None else None
    res["playback_package"] = ms_pkg
    res["is_playing"] = state == 3
    res["terminal"] = None

    if deep and pkg:
        rc, dump = adb("shell", "uiautomator dump /sdcard/tvsala_ui.xml", timeout=45)
        if "dumped" in dump:
            rc, xml = adb("shell", "cat /sdcard/tvsala_ui.xml", timeout=30)
            texts = []
            for part in xml.split("text=\"")[1:]:
                val = part.split("\"", 1)[0].strip()
                if val and val not in texts:
                    texts.append(val)
            res["screen_text"] = texts[:20]
        if pkg in DRM_APPS and res.get("is_playing"):
            res["screen_text_note"] = "app com DRM: o ecra mostra so o video, sem texto"

    return res


def describe(tv, sessions, err):
    """Linha PT-BR curta com o que esta a passar na TV da sala."""
    if not tv.get("reachable"):
        return f"TV da sala: sem resposta por ADB ({tv.get('connection')})."
    screen_state = tv.get("screen_state")
    if not tv.get("tv_on"):
        app = tv.get("app")
        if screen_state == "screensaver":
            return f"TV da sala: ligada mas em screensaver (a dormir) — {app}."
        return f"TV da sala: em standby/desligada (ultima app: {app or 'desconhecida'})."

    app = tv.get("app") or "app desconhecido"
    jelly = [s for s in (sessions or []) if s.get("device") == TV_DEVICE_NAME]

    if jelly and jelly[0]["playing"]:
        s = jelly[0]
        who = s.get("title") or "?"
        if s.get("series"):
            who = f"{s['series']} — {who}"
        pos = s.get("position_s")
        extra = f", {pos // 60}min{pos % 60:02d}s" if pos else ""
        state = "em pausa" if s.get("paused") else "a tocar"
        return f"TV da sala: Jellyfin {state} {who}{extra}."

    st = tv.get("playback_state")
    pos = tv.get("playback_position_s")
    if st == "a tocar":
        extra = f", {pos // 60}min{pos % 60:02d}s" if pos else ""
        return f"TV da sala: ligada, a tocar em {app}{extra}."
    if st == "em pausa":
        return f"TV da sala: ligada, {app} em pausa."
    return f"TV da sala: ligada, {app} aberto, nada a tocar."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="imprime so JSON")
    ap.add_argument("--deep", action="store_true", help="le tambem o texto do ecra")
    ap.add_argument("--state-file", help="ficheiro para comparar/detetar mudancas")
    args = ap.parse_args()

    tv = read_tv(deep=args.deep)
    sessions, jf_err = jellyfin_sessions()
    summary = describe(tv, sessions, jf_err)

    payload = {
        "summary": summary,
        "tv": tv,
        "jellyfin_sala": [s for s in (sessions or []) if s.get("device") == TV_DEVICE_NAME],
        "jellyfin_error": jf_err,
        "jellyfin_sessions_all": sessions or [],
    }

    if args.state_file:
        sig = {
            "tv_on": tv.get("tv_on"),
            "app": tv.get("app"),
            "state": tv.get("playback_state"),
            "jellyfin_title": (payload["jellyfin_sala"][0].get("title") if payload["jellyfin_sala"] else None),
        }
        prev = None
        if os.path.exists(args.state_file):
            try:
                prev = json.load(open(args.state_file))
            except Exception:
                prev = None
        payload["changed"] = prev != sig
        payload["previous"] = prev
        with open(args.state_file, "w") as fh:
            json.dump(sig, fh, ensure_ascii=False, indent=1)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(summary)
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
