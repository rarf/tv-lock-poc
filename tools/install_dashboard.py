#!/usr/bin/env python3
"""Install a Home Assistant storage-mode dashboard correctly.

PITFALL (cost a session on 2026-09-30): writing `.storage/lovelace.<id>` by hand
with `{"version": 1, "data": <dashboard>}` produces a dashboard that the frontend
reports as "Unknown error / Recarregar Interface". HA resolves storage dashboards
via `CONFIG_STORAGE_KEY = "lovelace.{}"` and expects a StoreFile envelope:

    {"version": N, "minor_version": M, "key": "lovelace.<id>",
     "data": {"config": {...actual lovelace config...}}}

The dashboard metadata (title/path/icon) belongs in
`.storage/lovelace_dashboards` -> data.items[], NOT inside the config file.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SRC = PROJECT / "lovelace-tv" / "dashboard-tv-lock.json"
CONTAINER = "casa-ha"
STORAGE = "/config/.storage"


def sh(*args: str) -> str:
    return subprocess.run(["docker", "exec", CONTAINER, *args],
                          capture_output=True, text=True, timeout=120).stdout


def read_store(name: str) -> dict:
    out = sh("cat", f"{STORAGE}/{name}")
    if not out.strip():
        raise SystemExit(f"nao consegui ler {name}")
    return json.loads(out)


def write_store(name: str, store: dict) -> None:
    payload = json.dumps(store, ensure_ascii=False, indent=2)
    sh("sh", "-c", f"cat > {STORAGE}/{name} <<'HEOF'\n{payload}\nHEOF")
    sh("chmod", "644", f"{STORAGE}/{name}")


def main() -> int:
    cfg = json.loads(SRC.read_text())
    dash_id = "tv_lock"
    url_path = cfg["path"]
    meta = {k: cfg[k] for k in ("title", "icon", "show_in_sidebar", "require_admin")}

    # 1) dashboard list
    reg = read_store("lovelace_dashboards")
    reg.setdefault("version", 1)
    reg.setdefault("minor_version", 1)
    reg.setdefault("key", "lovelace_dashboards")
    items = reg.get("data", {}).get("items", [])
    entry = {"id": dash_id, "url_path": url_path, "mode": "storage", **meta}
    items[:] = [i for i in items if i.get("id") != dash_id]
    items.append(entry)
    reg["data"] = {"items": items}
    write_store("lovelace_dashboards", reg)
    print(f"lovelace_dashboards: registado {dash_id} -> {url_path}")

    # 2) the dashboard config, in the StoreFile envelope
    store = {
        "version": 1,
        "minor_version": 1,
        "key": f"lovelace.{dash_id}",
        "data": {"config": {k: v for k, v in cfg.items()
                             if k not in ("title", "icon", "show_in_sidebar",
                                          "require_admin")}},
    }
    write_store(f"lovelace.{dash_id}", store)
    print(f"lovelace.{dash_id}: config gravada no envelope correcto")
    print(json.dumps(store, ensure_ascii=False)[:120] + " ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
