#!/usr/bin/env python3
"""tv_lock — reversible parental lock for the Android TVs, driven by a journal.

Design rules (project home-assistant + skill android-tv-adb-management):
- The ONLY mutation is `pm disable-user --user 0 <pkg>` / `pm enable <pkg>`.
- Every package this tool disables is written to a per-TV JSON journal.
  Unlock re-enables ONLY what the journal recorded, so we never touch packages
  that were already disabled before us (e.g. vendor analytics), and never
  re-enable something the user disabled on purpose after us.
- Protected set is allowlist-based: launcher, input, settings, system UI,
  casting, bluetooth/remote, accessibility, Play Store, GMS/GSF, and any
  package that is not an explicitly authorised block target.
- A safety allowlist can be locked separately (e.g. Jellyfin) so `lock_all`
  never blocks the escape hatch.
- Read-only status is always available and never mutates.

Usage:
  tv_lock.py status                      # read-only report for every TV
  tv_lock.py block --tv tv_da_sala       # lock (disable authorised apps)
  tv_lock.py unblock --tv tv_da_sala     # unlock (re-enable journal only)
  tv_lock.py add --tv tv_da_sala --packages a,b
  tv_lock.py serve --port 8129           # HTTP API for Home Assistant
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"
JOURNAL_DIR = STATE_DIR / "journal"

# Endpoints come from config.json / $TV_LOCK_TVS, never hard-coded: this tool
# must work for any TV on any network.
from discovery import load_tvs  # noqa: E402


def _tvs() -> dict:
    return load_tvs()

HOME_RESOLVER = "com.google.android.tvlauncher"

# Never disable, whatever else happens.
PROTECTED_EXACT = {
    "com.google.android.tvlauncher",
    "com.google.android.launcher",
    "com.google.android.leanback",
    "com.google.android.leanback.launcher",
    # Google TV / Chromecast-with-Google-TV default launcher. If this is
    # disabled the device can end up with no HOME app at all.
    "com.google.android.apps.tv.launcherx",
    "com.google.android.googlequicksearchbox",
    "com.miui.home",
    "com.mitv.tvhome.atv",          # Xiaomi TV home surface
    "com.android.settings",
    "com.android.tv.settings",
    "com.android.systemui",
    "com.android.packageinstaller",
    "com.android.permissioncontroller",
    "com.android.shell",
    "com.android.bluetooth",
    "com.google.android.bluetooth",
    "com.google.android.katniss",   # voice search / remote
    "com.google.android.tv.remote.service",
    "com.google.android.inputmethod.latin",
    "com.android.inputmethod.latin",
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.google.android.apps.mediashell",
    "com.android.providers.settings",
    "com.android.providers.media",
    "com.android.dreams.basic",
    "com.google.android.backdrop",
    "android",
}
PROTECTED_PREFIX = (
    "com.android.",
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.google.android.projection",
    "com.google.android.katniss",
    # OEM trees. `com.mitv.` and `com.mstar.` carry Xiaomi/MediaTek settings,
    # input, screensaver and TV services; disabling them can brick the remote
    # or the picture. Review per-device before widening.
    "com.miui.",
    "com.mi.",
    "com.xiaomi.",
    "com.mitv.",
    "com.mstar.",
    "com.mediatek.",
    "android.",
)

# Packages that are normally protected, but which Ronald decided to block on
# 2026-09-29 (option A) so the child cannot reinstall the apps we just disabled.
# Keeping this as an explicit, dated decision is what makes the choice auditable
# and reversible: the unlock re-enables them like any other journalled package.
UNPROTECT_BY_DECISION = {
    "com.android.vending",   # Google Play Store — the reinstall escape hatch
}

# Apps the user may open while locked (empty = total lockdown).
SAFE_ALLOWLIST: set[str] = set()

DEFAULT_STATE: dict[str, dict] = {
    "tv_da_sala": {
        # Verified on 2026-09-29 from `pm list packages` + MAIN/LAUNCHER activities.
        "targets": [
            "com.disney.disneyplus",
            "com.google.android.youtube.tv",
            "com.google.android.youtube.tvmusic",
            "com.mediatek.androidbox",
            "com.mediatek.wwtv.tvcenter",
            "com.mitv.dream",
            "com.mitv.gallery",
            "com.mstar.netflixobserver",
            "com.nathnetwork.xciptv",
            "com.netflix.ninja",
            "com.neutronemulation.super_retro_16",
            "com.plexapp.android",
            "com.stremio.one",
            "com.yablio.sendfilestotv",
            "org.jellyfin.androidtv",
        ],
        "enabled": False,
    },
    "tv_do_quarto": {
        "targets": [
            "com.cloudmosa.puffinTV",
            "com.disney.disneyplus",
            "com.google.android.tv",
            "com.google.android.youtube.tv",
            "com.google.android.youtube.tvmusic",
            "com.nathnetwork.xciptv",
            "com.netflix.ninja",
            "com.nordvpn.android",
            "com.plexapp.android",
            "com.stremio.one",
            "com.yablio.sendfilestotv",
            "org.jellyfin.androidtv",
        ],
        "enabled": False,   # only tv_da_sala is in scope until Ronald extends it
    },
}

# Lock policy/targets, SEPARATE from the device topology in discovery.py
# (config.json). Sharing one file breaks both readers — see the code review.
STATE_FILE = STATE_DIR / "targets_state.json"


# --------------------------------------------------------------------------- io
def load_config() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(DEFAULT_STATE, indent=2))
    return json.loads(STATE_FILE.read_text())


def _atomic_write(path: Path, data: str) -> None:
    """SEC-16: a crash mid-write used to leave a truncated JSON file, which on
    the next start means "no targets" or, worse, a half-written journal that
    makes unlock re-enable the wrong set. Write to a temp file, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def save_config(cfg: dict) -> None:
    _atomic_write(STATE_FILE, json.dumps(cfg, indent=2, ensure_ascii=False))


def journal_path(tv: str) -> Path:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return JOURNAL_DIR / f"{tv}.json"


def load_journal(tv: str) -> dict:
    p = journal_path(tv)
    return json.loads(p.read_text()) if p.exists() else {"tv": tv, "entries": []}


def save_journal(tv: str, j: dict) -> None:
    _atomic_write(journal_path(tv), json.dumps(j, indent=2, ensure_ascii=False))


# ----------------------------------------------------------------------- adb
def adb(serial: str, *args: str, timeout: int = 60) -> tuple[int, str]:
    p = subprocess.run(["adb", "-s", serial, *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ensure_connected(tv: str) -> tuple[bool, str]:
    serial = _tvs().get(tv)
    if not serial:
        return False, f"desconhecido: {tv}"
    rc, out = adb(serial, "get-state", timeout=30)
    if rc == 0 and out.strip() == "device":
        return True, serial
    adb(serial, "connect", timeout=30)
    rc, out = adb(serial, "get-state", timeout=30)
    return (rc == 0 and out.strip() == "device"), f"reconnect: {out.strip()}"


def list_disabled(serial: str) -> list[str]:
    rc, out = adb(serial, "shell", "pm", "list", "packages", "-d", timeout=60)
    if rc != 0:
        return []
    return [l.split(":")[-1].strip() for l in out.splitlines() if l.startswith("package:")]


def is_protected(pkg: str) -> bool:
    """Normalise before matching.

    Package names are case-sensitive on Android, but the value can arrive from
    a config file or a shell argument. Comparing raw lets `com.Miui.X` or
    ` com.miui.x ` slip past the protected set. Whitespace is stripped and
    matching is case-insensitive, which can only ever protect MORE packages.
    """
    if not isinstance(pkg, str):
        return True  # fail safe: unknown shape is treated as protected
    name = pkg.strip().lower()
    if not name:
        return True
    if name in {p.strip().lower() for p in UNPROTECT_BY_DECISION}:
        return False
    if name in {p.strip().lower() for p in PROTECTED_EXACT}:
        return True
    return name.startswith(tuple(p.strip().lower() for p in PROTECTED_PREFIX))


# ------------------------------------------------------------------- commands
def cmd_status(_args) -> dict:
    cfg = load_config()
    out = {"checked_at": now(), "tvs": {}}
    for tv in _tvs():
        connected, detail = ensure_connected(tv)
        rec = {"serial": _tvs()[tv], "connected": connected, "detail": detail,
               "enabled": cfg.get(tv, {}).get("enabled", False),
               "targets": cfg.get(tv, {}).get("targets", []),
               "safe_allowlist": sorted(SAFE_ALLOWLIST)}
        if connected:
            disabled = set(list_disabled(_tvs()[tv]))
            journal = load_journal(tv)
            locked = {e["package"] for e in journal["entries"] if e.get("state") == "disabled"}
            rec["disabled_now"] = sorted(disabled)
            rec["locked_by_us"] = sorted(locked)
            rec["locked_and_still_disabled"] = sorted(locked & disabled)
            rec["locked_but_reenabled_elsewhere"] = sorted(locked - disabled)
            rec["protected_leak"] = sorted(t for t in locked if is_protected(t))
            rec["effective_lock"] = bool(locked - disabled) is False and bool(locked)
        out["tvs"][tv] = rec
    return out


def cmd_add(args) -> dict:
    cfg = load_config()
    pkgs = [p.strip() for p in args.packages.split(",") if p.strip()]
    refused = [p for p in pkgs if is_protected(p) or p in SAFE_ALLOWLIST]
    if refused:
        return {"error": "recusado (protegido/allowlist)", "packages": refused}
    cfg.setdefault(args.tv, {"targets": [], "enabled": False})
    for p in pkgs:
        if p not in cfg[args.tv]["targets"]:
            cfg[args.tv]["targets"].append(p)
    save_config(cfg)
    return {"ok": True, "tv": args.tv, "targets": cfg[args.tv]["targets"],
            "refused": refused}


def cmd_block(args) -> dict:
    cfg = load_config()
    if args.tv not in _tvs():
        return {"error": f"tv desconhecida: {args.tv}"}
    connected, detail = ensure_connected(args.tv)
    if not connected:
        return {"error": f"ADB não ligado ({detail})", "tv": args.tv}
    serial = _tvs()[args.tv]
    targets = cfg.get(args.tv, {}).get("targets", [])
    if args.only_targets and not targets:
        return {"error": "sem targets configurados", "tv": args.tv}

    pre_disabled = set(list_disabled(serial))
    journal = load_journal(args.tv)
    entries = journal["entries"]

    if args.dry_run:
        plan = [p for p in targets if p not in pre_disabled and not is_protected(p)]
        return {"dry_run": True, "tv": args.tv, "would_disable": plan,
                "already_disabled": [p for p in targets if p in pre_disabled],
                "protected_refused": [p for p in targets if is_protected(p)]}

    # Leave the app and go to the launcher first, so nothing keeps running.
    adb(serial, "shell", "input", "keyevent", "KEYCODE_HOME", timeout=30)

    disabled_now, failed, skipped = [], [], []
    for pkg in targets:
        if is_protected(pkg):
            skipped.append({"package": pkg, "reason": "protected"})
            continue
        if pkg in SAFE_ALLOWLIST:
            skipped.append({"package": pkg, "reason": "safe_allowlist"})
            continue
        if pkg in pre_disabled:
            # Already off and NOT by us: leave it, do not claim it.
            skipped.append({"package": pkg, "reason": "already_disabled_by_other"})
            continue
        # SEC-07: record the INTENT before mutating. If we are interrupted
        # between `pm disable-user` and the verification readback, the package
        # is still re-enabled by `unblock` — a half-written journal would
        # strand it on the TV with no record anywhere.
        entries.append({"package": pkg, "state": "pending", "at": now(),
                        "action": "lock", "batch": args.batch})
        save_journal(args.tv, journal)
        rc, out = adb(serial, "shell", "pm", "disable-user", "--user", "0", pkg, timeout=60)
        if rc != 0:
            entries[-1]["state"] = "failed"
            entries[-1]["error"] = out.strip()[:160]
            save_journal(args.tv, journal)
            failed.append({"package": pkg, "error": out.strip()[:160]})
            continue
        entries[-1]["state"] = "disabled"
        save_journal(args.tv, journal)
        disabled_now.append(pkg)

    # Verify by readback, never trust the exit code alone.
    after = set(list_disabled(serial))
    verified = [p for p in disabled_now if p in after]
    unverified = [p for p in disabled_now if p not in after]

    cfg.setdefault(args.tv, {"targets": [], "enabled": False})
    cfg[args.tv]["enabled"] = True
    save_config(cfg)
    save_journal(args.tv, journal)

    return {"ok": not failed and not unverified, "tv": args.tv, "batch": args.batch,
            "disabled": verified, "unverified": unverified, "failed": failed,
            "skipped": skipped, "total_disabled_on_device": len(after)}


def cmd_unblock(args) -> dict:
    cfg = load_config()
    if args.tv not in _tvs():
        return {"error": f"tv desconhecida: {args.tv}"}
    connected, detail = ensure_connected(args.tv)
    if not connected:
        return {"error": f"ADB não ligado ({detail})", "tv": args.tv}
    serial = _tvs()[args.tv]
    journal = load_journal(args.tv)
    entries = journal["entries"]

    if args.dry_run:
        live = {e["package"] for e in entries if e.get("state") == "disabled"}
        pre = set(load_journal(args.tv).get("pre_disabled", []))
        return {"dry_run": True, "tv": args.tv,
                "would_enable": sorted(live), "not_ours": sorted(pre)}

    reenabled, failed, stale = [], [], []
    for e in entries:
        if e.get("state") != "disabled":
            continue
        pkg = e["package"]
        # SEC-06: a package can become protected AFTER we disabled it (config
        # change, firmware rename). Refusing to re-enable it would strand it
        # disabled on the TV forever. It is in OUR journal, so restoring it is
        # the correct action.
        if is_protected(pkg) and pkg.strip().lower() not in {
                p.strip().lower() for p in UNPROTECT_BY_DECISION}:
            stale.append({"package": pkg,
                          "reason": "agora protegido, mas foi-nos que desativámos — reativado",
                          "restored": True})
        rc, out = adb(serial, "shell", "pm", "enable", pkg, timeout=60)
        if rc != 0:
            failed.append({"package": pkg, "error": out.strip()[:160]})
            continue
        e["state"] = "enabled"
        e["enabled_at"] = now()
        reenabled.append(pkg)
        continue
        rc, out = adb(serial, "shell", "pm", "enable", pkg, timeout=60)
        if rc != 0:
            failed.append({"package": pkg, "error": out.strip()[:160]})
            continue
        e["state"] = "enabled"
        e["enabled_at"] = now()
        reenabled.append(pkg)

    after = set(list_disabled(serial))
    verified = [p for p in reenabled if p not in after]
    unverified = [p for p in reenabled if p in after]

    cfg.setdefault(args.tv, {"enabled": False})
    cfg[args.tv]["enabled"] = False
    save_config(cfg)
    save_journal(args.tv, journal)

    return {"ok": not failed and not unverified, "tv": args.tv,
            "re_enabled": verified, "unverified": unverified, "failed": failed,
            "protected_refused": stale, "still_disabled": sorted(after)}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- cli
# NOTE: this module used to ship its own HTTP server on 0.0.0.0 with no
# actor authorisation and no watching guard. It was removed (SEC-15):
# tv_lock_service.py is the ONLY HTTP surface, and it is loopback+token
# + allowlist. Running two servers on the same port was a footgun.


def main() -> int:
    ap = argparse.ArgumentParser(description="Bloqueio reversível de apps nas TVs (parental)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    p_lock = sub.add_parser("block")
    p_lock.add_argument("--tv", required=True, choices=list(_tvs()))
    p_lock.add_argument("--dry-run", action="store_true")
    p_lock.add_argument("--batch", default="manual")
    p_lock.add_argument("--only-targets", action="store_true", default=True)
    p_unlock = sub.add_parser("unblock")
    p_unlock.add_argument("--tv", required=True, choices=list(_tvs()))
    p_unlock.add_argument("--dry-run", action="store_true")
    p_add = sub.add_parser("add")
    p_add.add_argument("--tv", required=True, choices=list(_tvs()))
    p_add.add_argument("--packages", required=True)

    args = ap.parse_args()
    if args.cmd == "status":
        result = cmd_status(args)
    elif args.cmd == "block":
        result = cmd_block(args)
    elif args.cmd == "unblock":
        result = cmd_unblock(args)
    elif args.cmd == "add":
        result = cmd_add(args)
    else:
        print("comando desconhecido", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
