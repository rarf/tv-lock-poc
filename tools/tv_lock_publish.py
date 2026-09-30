#!/usr/bin/env python3
"""tv_lock_publish — push the current lock/guard state into Home Assistant.

Home Assistant's `command_line` platform is not available in this build, so the
sensors are fed by a small poller that calls the service and sets states via
the HA REST API. It needs only a long-lived HA token, read from the env file
created at setup (never printed).

State published:
  sensor.tv_sala_apps_bloqueadas_raw  -> integer, disabled app count
  sensor.tv_sala_bloqueada_raw        -> "on" / "off"
  sensor.tv_sala_em_uso_raw          -> "yes" / "no"
  sensor.tv_sala_a_ver_raw           -> title currently playing, or "nada"
  sensor.tv_sala_destrava_ate_raw    -> ISO expiry, or "travada"

Run:  python3 tools/tv_lock_publish.py --once      (single refresh)
      python3 tools/tv_lock_publish.py             (poll every 60 s)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tv_lock  # noqa: E402
from tv_watch_guard import inspect as guard_inspect  # noqa: E402

HA = os.environ.get("HA_URL", "http://127.0.0.1:8123")
TOKEN_FILE = HERE / "state" / "ha_token"
SERVICE = os.environ.get("TV_LOCK_SERVICE", "http://127.0.0.1:8129")
LOCK_TOKEN_FILE = HERE / "state" / "api_token"
TV = "tv_da_sala"


def ha_token() -> str:
    return TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""


def lock_token() -> str:
    return LOCK_TOKEN_FILE.read_text().strip() if LOCK_TOKEN_FILE.exists() else ""


def service(path: str) -> dict:
    req = urllib.request.Request(SERVICE + path,
                                 headers={"X-TV-Lock-Token": lock_token()})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read() or b"{}")


def set_state(entity: str, value, attributes: dict | None = None) -> str:
    tok = ha_token()
    if not tok:
        return "sem token HA"
    body = {"state": value, "attributes": attributes or {}}
    req = urllib.request.Request(
        f"{HA}/api/states/{entity}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode(errors='replace')[:120]}"
    except Exception as e:
        return f"ERRO {type(e).__name__}: {e}"


def collect() -> dict:
    st = service(f"/status?tv={TV}")
    rec = st.get("tvs", {}).get(TV, {})
    disabled = len(rec.get("disabled_now", []) or [])
    g = st.get("guard") or guard_inspect(TV)
    window = st.get("unlock_window") or {}
    jf = g.get("jellyfin") or []
    ms = g.get("media_session") or {}
    playing = [x.get("title") for x in jf if not x.get("paused")]
    if not playing and ms.get("state") == "PLAYING":
        playing = [ms.get("package") or "reprodução"]
    aviso = st.get("aviso") or ""
    return {
        "aviso": aviso,
        "disabled": disabled,
        "locked": disabled > 2,
        "in_use": g.get("verdict") in ("IN_USE", "PAUSED"),
        "playing": ", ".join(p for p in playing if p) or "nada",
        "expires": window.get("expires_at", "travada"),
        "verdict": g.get("verdict"),
    }


def publish(state: dict) -> None:
    common = {"friendly_name": "TV da sala", "source": "tv_lock_publish",
              "icon": "mdi:tv"}
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
          f"bloqueadas={state['disabled']} travada={state['locked']} "
          f"em_uso={state['in_use']} a_ver={state['playing']!r} "
          f"destrava_ate={state['expires']}")
    for entity, value, extra in (
        ("sensor.tv_sala_apps_bloqueadas_raw", state["disabled"], {}),
        ("sensor.tv_sala_bloqueada_raw",
         "on" if state["locked"] else "off", {}),
        ("sensor.tv_sala_em_uso_raw",
         "yes" if state["in_use"] else "no", {}),
        ("sensor.tv_sala_a_ver_raw", state["playing"], {}),
        ("sensor.tv_sala_destrava_ate_raw", state["expires"], {}),
        ("sensor.tv_sala_aviso_raw", state["aviso"] or "sem avisos", {}),
    ):
        attrs = dict(common)
        attrs.update(extra)
        print(f"   {entity} = {value!r} -> {set_state(entity, value, attrs)}")


def main() -> int:
    # line-buffered: o output tem de aparecer no log a cada ciclo, mesmo
    # quando o stdout e um ficheiro (block buffering).
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    if not ha_token():
        print("FALTA token do Home Assistant em tools/state/ha_token", file=sys.stderr)
        print("Cria-o no HA: Perfil -> Tokens de longa duração (Nao copiar para o chat).",
              file=sys.stderr)
        return 2

    while True:
        try:
            publish(collect())
            sys.stdout.flush()
        except Exception as e:
            print(f"ERRO no ciclo: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if args.once:
            return 0
        # Print the first cycle immediately, then sleep. `publish()` uses
        # print() without flush and stdout is block-buffered when redirected to
        # a file, so the output only appeared once the buffer filled.
        time.sleep(max(15, args.interval))


if __name__ == "__main__":
    sys.exit(main())
