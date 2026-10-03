#!/usr/bin/env python3
"""tv_lock_service — the HTTP surface Home Assistant and Telegram call.

Adds three things on top of the CLI in `tv_lock.py`:
  1. a WATCH GUARD: `lock` is refused while the TV is playing or paused, and
     the response carries what is on screen so the caller can warn the adult
     and offer "continue" or "force lock".
  2. a SELF-EXPIRING unlock: `unlock` records an expiry (default 2h) and a
     background watchdog re-locks automatically when it lapses, so an unlock
     can never be forgotten.
  3. an AUDIT log: every action, who asked, when, and the result.

Endpoints (all mutations require header `X-TV-Lock-Token`):
  GET  /health
  GET  /status?tv=<id>          read-only: lock state + watch verdict
  POST /guard?tv=<id>           read-only: only the watch verdict
  POST /lock   {tv, force?}     block; refuses if in use unless force=true
  POST /unlock {tv, minutes?}   unblock for N minutes (default 120)
  POST /maintenance {tv, minutes?}  temporary allowlist: re-enable the apps
                                    the user needs (e.g. Play Store) without
                                    unlocking the streaming apps

State lives next to this file; the token is read from `tools/state/api_token`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tv_lock  # noqa: E402
from discovery import load_tvs  # noqa: E402
from tv_watch_guard import inspect as guard_inspect  # noqa: E402
import hdmi_lock  # noqa: E402

STATE_DIR = HERE / "state"
TOKEN_FILE = STATE_DIR / "api_token"
WINDOW_FILE = STATE_DIR / "unlock_window.json"
MAINT_FILE = STATE_DIR / "maintenance.json"
AUDIT_FILE = STATE_DIR / "audit.log"

DEFAULT_UNLOCK_MINUTES = 120
_wake = threading.Event()


# ------------------------------------------------------------------------ utils
ALLOWED_USERS_FILE = STATE_DIR / "allowed_users.json"


def _tv_from(payload: dict, params: dict | None) -> str:
    """TV id from the query string first, then the JSON body."""
    if params:
        raw = params.get("tv")
        if raw:
            return str(raw).strip().lower()
    return str(payload.get("tv") or "").strip().lower()


def allowed_users() -> dict:
    """Map of lowercased HA user_id -> display name (Ronald and Márcia only)."""
    return read_json(ALLOWED_USERS_FILE, {})


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)
    path.chmod(0o600)


def audit(action: str, tv: str, actor: str, result: str, detail: str = "") -> None:
    line = {"at": iso(), "action": action, "tv": tv, "actor": actor,
            "result": result, "detail": detail[:400]}
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_FILE.open("a") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def token() -> str:
    return TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""


# ------------------------------------------------------------------- unlock window
def load_window() -> dict:
    return read_json(WINDOW_FILE, {})


def window_active() -> dict | None:
    w = load_window()
    if not w or not w.get("expires_at"):
        return None
    if now() >= datetime.fromisoformat(w["expires_at"]):
        return None
    return w


def set_window(tv: str, minutes: int, actor: str) -> dict:
    start, end = now(), now() + timedelta(minutes=minutes)
    w = {"tv": tv, "opened_at": iso(start), "expires_at": iso(end),
         "minutes": minutes, "actor": actor}
    write_json(WINDOW_FILE, w)
    audit("unlock_window_set", tv, actor, "ok", f"{minutes} min until {iso(end)}")
    _wake.set()
    return w


def clear_window(actor: str, reason: str) -> None:
    w = load_window()
    if w:
        audit("unlock_window_clear", w.get("tv", "?"), actor, "ok", reason)
    if WINDOW_FILE.exists():
        WINDOW_FILE.unlink()
    _wake.set()


def _close_expired_maintenance() -> None:
    """SEC-12: re-disable whatever maintenance mode opened, when it lapses."""
    m = read_json(MAINT_FILE, {})
    if not m or not m.get("expires_at"):
        return
    if now() < datetime.fromisoformat(m["expires_at"]):
        return
    tv = m.get("tv")
    if tv in tv_lock.TVS:
        import argparse as _ap
        tv_lock.ensure_connected(tv)
        for pkg in m.get("packages", []):
            tv_lock.adb(tv_lock.TVS[tv], "shell", "pm", "disable-user",
                        "--user", "0", pkg, timeout=60)
        audit("maintenance_expired", tv, "watchdog", "ok",
              f"re-bloqueado: {','.join(m.get('packages', []))}")
    MAINT_FILE.unlink(missing_ok=True)
    _wake.set()


def _watchdog() -> None:
    """Re-lock automatically when the unlock or maintenance window lapses."""
    while True:
        _close_expired_maintenance()
        w = load_window()
        if w and w.get("expires_at"):
            if now() >= datetime.fromisoformat(w["expires_at"]):
                tv = w.get("tv")
                if tv in tv_lock._tvs():
                    import argparse as _ap
                    tv_lock.ensure_connected(tv)
                    res = tv_lock.cmd_block(_ap.Namespace(tv=tv, dry_run=False,
                                                          batch="auto-relock",
                                                          only_targets=True))
                    audit("auto_relock", tv, "watchdog",
                          "ok" if res.get("ok") else "error",
                          f"janela expirou; {len(res.get('disabled', []))} apps; "
                          f"falhas={res.get('failed')}")
                    if res.get("ok"):
                        clear_window("watchdog", "janela expirou — re-trava automatica")
                    else:
                        # SEC-11: a failed re-lock must NOT delete the window,
                        # otherwise the TV is silently left unlocked forever.
                        audit("auto_relock_failed", tv, "watchdog", "error",
                              f"janela mantida; {res.get('failed')}")
        _wake.wait(timeout=30)
        _wake.clear()


# --------------------------------------------------------------------- handlers
def handle_guard(tv: str) -> dict:
    return guard_inspect(tv)


def _hdmi(fn, tv: str, cut: bool = False) -> dict:
    """Run an HDMI action without letting it break the app lock.

    The app lock is the primary control and must complete even if the TV is
    unreachable for HDMI purposes. Failures are reported, never swallowed.
    """
    try:
        return fn(tv, cut_screen=cut) if cut else fn(tv)
    except Exception as e:  # noqa: BLE001
        audit("hdmi_error", tv, "service", "error", f"{type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def handle_lock_safe(payload: dict, actor: str, params: dict | None = None,
        req_headers: dict | None = None) -> tuple[dict, int]:
    """Same as handle_lock, but never returns 4xx/5xx.

    Home Assistant aborts a script sequence on a rest_command error, so the
    user would never see the "está a ver X" warning. This variant always
    returns 200 and puts the verdict in the body, letting the script branch.
    """
    tv = _tv_from(payload, params)
    if tv not in tv_lock._tvs():
        return {"ok": False, "blocked": "unknown_tv", "message": f"tv desconhecida: {tv}"}, 200
    hdr_force = (req_headers or {}).get("X-TV-Lock-Force", "")
    force = payload.get("force") is True or str(hdr_force).lower() == "true"

    w = window_active()
    if w and w.get("tv") == tv:
        if not force:
            audit("lock_refused_window", tv, actor, "blocked",
                  f"destrava ate {w['expires_at']}")
            return {"ok": False, "blocked": "unlock_window",
                    "message": f"A TV está destravada até {w['expires_at']}.",
                    "window": w}, 200
        # force means "travar agora": close the window first, otherwise the
        # watchdog would re-open/lock on a window the adult just cancelled.
        audit("lock_override_window", tv, actor, "ok",
              f"forcado apesar da janela ate {w['expires_at']}")
        clear_window(actor, "trava forcada durante a janela de destrava")

    g = guard_inspect(tv)
    if not g["safe_to_lock"]:
        titles = [x.get("title") for x in g.get("jellyfin", []) if x.get("title")]
        what = ", ".join(titles) if titles else (g["media_session"].get("package") or "reprodução")
        audit("lock_refused_in_use", tv, actor, "blocked",
              f"{g['reason']} | {what}")
        return {"ok": False, "blocked": "in_use", "watching": what,
                "verdict": g["verdict"],
                "message": f"A TV está em uso: {what}."}, 200

    import argparse as _ap
    res = tv_lock.cmd_block(_ap.Namespace(tv=tv, dry_run=False,
                                          batch=f"lock-{actor}", only_targets=True))
    audit("lock", tv, actor, "ok" if res.get("ok") else "error",
          f"{len(res.get('disabled', []))} apps; force={force}")
    # The HDMI lock travels with the app lock: same action, same authorisation.
    hd = _hdmi(hdmi_lock.engage, tv, cut=True)
    return {"ok": bool(res.get("ok")), "blocked": None,
            "disabled": res.get("disabled", []),
            "hdmi": hd,
            "message": f"TV da sala travada ({len(res.get('disabled', []))} apps)."}, 200


def handle_lock(payload: dict, actor: str, params: dict | None = None,
        req_headers: dict | None = None) -> tuple[dict, int]:
    tv = _tv_from(payload, params)
    if tv not in tv_lock._tvs():
        return {"error": f"tv desconhecida: {tv}"}, 400
    # Safety default: only an explicit `force: true` may cut playback. A missing
    # or malformed field must NEVER be treated as True — a caller that loses its
    # templating would otherwise silently override the watching guard.
    hdr_force = (req_headers or {}).get("X-TV-Lock-Force", "")
    force = payload.get("force") is True or str(hdr_force).lower() == "true"

    w = window_active()
    # `force` means the adult explicitly chose to lock, so it must also override
    # an open unlock window. Otherwise an expired-but-unnoticed unlock would keep
    # the TV open forever and force would be a no-op.
    if w and w.get("tv") == tv and not force:
        audit("lock_refused_window", tv, actor, "error",
              f"destrava ate {w['expires_at']}")
        return {"error": "a tv esta destravada neste momento",
                "window": w, "safe_to_lock": False}, 409
    if w and w.get("tv") == tv:
        audit("lock_override_window", tv, actor, "ok",
              f"forcado apesar da janela ate {w['expires_at']}")
        clear_window(actor, "trava forcada durante a janela de destrava")

    g = guard_inspect(tv)
    if not g["safe_to_lock"] and not force:
        audit("lock_refused_in_use", tv, actor, "refused", g["reason"])
        return {"ok": False, "refused": True, "guard": g,
                "message": "a tv esta em uso — confirme se quer travar assim mesmo",
                "force_supported": True}, 409

    import argparse as _ap
    res = tv_lock.cmd_block(_ap.Namespace(tv=tv, dry_run=False,
                                          batch=f"lock-{actor}", only_targets=True))
    audit("lock", tv, actor, "ok" if res.get("ok") else "error",
          f"{len(res.get('disabled', []))} apps; force={force}")
    # HDMI lock travels with the app lock: same action, same authorisation.
    hd = _hdmi(hdmi_lock.engage, tv, cut=True)
    res["hdmi"] = hd
    return res, 200 if res.get("ok") else 500


def handle_unlock(payload: dict, actor: str, params: dict | None = None,
                  req_headers: dict | None = None) -> tuple[dict, int]:
    tv = _tv_from(payload, params)
    if tv not in tv_lock._tvs():
        return {"error": f"tv desconhecida: {tv}"}, 400
    # HA evaluates a rest_command payload template before the script's `data`,
    # so `minutes` silently became the 120-minute default. The header is the
    # reliable channel; the body stays as a fallback for direct calls.
    minutes_raw = (req_headers or {}).get("X-TV-Lock-Minutes")
    minutes = int(minutes_raw or payload.get("minutes") or DEFAULT_UNLOCK_MINUTES)
    minutes = max(5, min(minutes, 720))

    # Check transport BEFORE touching anything: an unreachable TV is a 503
    # (retryable), not a 500, and it must not open a window it cannot honour.
    connected, detail = tv_lock.ensure_connected(tv)
    if not connected:
        audit("unlock", tv, actor, "error", f"ADB indisponivel: {detail}")
        return {"error": f"ADB nao ligado ({detail})", "tv": tv}, 503

    g = guard_inspect(tv)
    import argparse as _ap
    res = tv_lock.cmd_unblock(_ap.Namespace(tv=tv, dry_run=False))
    if not res.get("ok"):
        audit("unlock", tv, actor, "error", "falhou a reativar")
        return res, 500
    w = set_window(tv, minutes, actor)
    audit("unlock", tv, actor, "ok",
          f"{len(res.get('re_enabled', []))} apps; {minutes} min")
    hd = _hdmi(lambda tv: hdmi_lock.release(tv, wake=False), tv)
    return {"ok": True, "tv": tv, "re_enabled": len(res.get("re_enabled", [])),
            "window": w, "expires_at": w["expires_at"],
            "hdmi": hd,
            "guarded": g["verdict"] != "FREE"}, 200


def handle_maintenance(payload: dict, actor: str, params: dict | None = None) -> tuple[dict, int]:
    """Temporarily re-enable the Play Store only, keeping the block on the
    streaming apps. This is the safe escape hatch Ronald needs to install or
    update something without handing the children the whole catalogue."""
    tv = _tv_from(payload, params)
    if tv not in tv_lock._tvs():
        return {"error": f"tv desconhecida: {tv}"}, 400
    minutes = int(payload.get("minutes") or 30)
    minutes = max(5, min(minutes, 120))
    packages = payload.get("packages") or ["com.android.vending"]
    protected = [p for p in packages if p not in tv_lock.UNPROTECT_BY_DECISION]
    if protected:
        return {"error": "modo manutencao so aceita pacotes explicitamente "
                         "decididos: com.android.vending", "recusado": protected}, 400
    connected, detail = tv_lock.ensure_connected(tv)
    if not connected:
        return {"error": f"ADB nao ligado ({detail})"}, 503
    done = []
    for p in packages:
        rc, out = tv_lock.adb(tv_lock._tvs()[tv], "shell", "pm", "enable", p, timeout=60)
        if rc == 0:
            done.append(p)
    audit("maintenance", tv, actor, "ok", f"{','.join(done)} por {minutes} min")
    return {"ok": bool(done), "tv": tv, "enabled": done, "minutes": minutes,
            "note": "as apps de streaming continuam bloqueadas"}, 200


def handle_aviso(payload: dict, actor: str, params: dict | None = None) -> tuple[dict, int]:
    """Publish the human-facing message for the dashboard.

    `persistent_notification` does not persist in this HA build, so the last
    message is kept in the state file and the publisher exposes it as a sensor.
    """
    tv = _tv_from(payload, params)
    if tv not in tv_lock._tvs():
        return {"ok": False}, 200
    msg = (payload.get("mensagem") or "").strip()
    write_json(WINDOW_FILE.parent / "aviso.json",
               {"tv": tv, "actor": actor, "mensagem": msg, "at": iso()})
    audit("aviso", tv, actor, "ok", msg)
    return {"ok": True, "mensagem": msg}, 200


def handle_status(tv: str | None) -> dict:
    base = tv_lock.cmd_status(None)
    out = {"checked_at": iso(), "unlock_window": window_active(),
           "tvs": base.get("tvs", {})}
    if tv and tv in tv_lock._tvs():
        out["guard"] = guard_inspect(tv)
    elif tv:
        for name in tv_lock._tvs():
            out.setdefault("guards", {})[name] = guard_inspect(name)
    return out


# -------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "tv-lock-service/1.0"
    protocol_version = "HTTP/1.1"

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        return (self.headers.get("X-TV-Lock-Token") or "").strip() == token()

    def _tv_of(self, payload: dict, params: dict) -> str:
        """Resolve the TV id.

        Home Assistant 2026.8.3 drops fields from a folded-block JSON payload
        when they sit next to a Jinja expression, so `tv` is also sent in the
        query string. The query string is always delivered, so prefer it and
        fall back to the body.
        """
        raw = params.get("tv") or payload.get("tv") or ""
        return str(raw).strip().lower()

    def _allowed_users(self) -> dict:
        return allowed_users()

    def _actor(self) -> str:
        """Identify WHO is acting.

        Security requirement (2026-09-30): only Ronald and Márcia may change the
        lock. Two problems had to be solved at once:

        1. Home Assistant's `rest_command` does NOT expose `user_id` in its
           template context (verified on 2026.8.3), so the caller cannot simply
           assert who it is. Trusting a client-supplied name would be useless.
        2. Therefore identity is proven by the caller presenting a long-lived
           Home Assistant API token that belongs to one of the two adult
           accounts. The token itself is the credential; `X-TV-User-Id` is only
           a hint that is cross-checked against the token's owner.

        `state/allowed_users.json` maps lowercased user_id -> name.
        `state/allowed_tokens.json` maps the token hash -> user_id.
        An unknown or missing token fails closed.
        """
        allowed = self._allowed_users()
        if not allowed:
            return ""
        token_map = read_json(STATE_DIR / "allowed_tokens.json", {})
        presented = (self.headers.get("X-HA-Token") or "").strip()
        if not presented:
            return ""
        digest = hashlib.sha256(presented.encode()).hexdigest()
        user_id = token_map.get(digest)
        if user_id is None:
            return ""
        user_id = user_id.lower()
        if user_id not in allowed:
            return ""
        return f"{allowed[user_id]} ({user_id[:8]})"

    def do_GET(self):  # noqa: N802
        path, _, qs = self.path.partition("?")
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if path == "/health":
            return self._json({"status": "ok", "time": iso()})
        if path in ("/status", "/guard"):
            if not self._auth():
                return self._json({"error": "unauthorized"}, 401)
            tv = params.get("tv")
            if path == "/guard":
                if tv not in tv_lock._tvs():
                    return self._json({"error": "tv desconhecida"}, 400)
                return self._json(handle_guard(tv))
            return self._json(handle_status(tv))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        path, _, qs = self.path.partition("?")
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if not self._auth():
            return self._json({"error": "unauthorized"}, 401)
        # Identify the real person behind the request. Anything not on the
        # allowlist is refused — a child account or a leaked token must never be
        # able to unlock or force-lock the TV.
        self._payload_user = str(payload.get("user_id") or params.get("user_id")
                                 or (self.headers.get("X-TV-User-Id") or "")).strip().lower()
        actor = self._actor()
        if not actor:
            audit("refused_unknown_user", str(payload.get("tv")),
                  self._payload_user or "anon", "denied",
                  "utilizador nao autorizado a mudar a trava")
            return self._json({"ok": False,
                               "error": "so Ronald e Marcia podem mudar a trava"}, 403)
        handlers = {
            "/lock": handle_lock,
            "/tentar-travar": handle_lock_safe,
            "/unlock": handle_unlock,
            "/maintenance": handle_maintenance,
            "/aviso": handle_aviso,
        }
        fn = handlers.get(path)
        if not fn:
            return self._json({"error": "not found"}, 404)
        # Pass the request headers too: the unlock duration travels there
        # because HA renders a rest_command payload before the script's data.
        try:
            result, code = fn(payload, actor, params, dict(self.headers))
        except TypeError:
            result, code = fn(payload, actor, params)
        self._json(result, code)

    def log_message(self, fmt, *a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8129)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if not TOKEN_FILE.exists():
        print(f"token ausente: {TOKEN_FILE}", file=sys.stderr)
        return 2

    threading.Thread(target=_watchdog, daemon=True).start()
    srv = HTTPServer((args.host, args.port), Handler)
    print(f"tv_lock_service em http://{args.host}:{args.port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
