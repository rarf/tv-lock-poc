#!/usr/bin/env python3
"""Read-only inventory of both Android TVs for the parental lock.

Collects, per device: identity, HOME resolver, launchable (MAIN/LAUNCHER) apps,
pre-existing disabled packages, protected-critical packages, storage/memory.
Writes a machine-readable baseline + a proposed block list + rollback commands.
READ-ONLY: no pm disable-user, no settings change, nothing mutating.
"""
import json, subprocess, sys, re, os
from datetime import datetime

SCRATCH = "/home/rarf/.hermes/cache/scratch"
OUT = os.path.join(SCRATCH, "tv_baseline.json")

# IPs rediscovered live (stale .153 no longer exists)
TVS = load_tvs()

# Never disable: launcher, input, settings, system core, casting, bluetooth/remote,
# accessibility, ADB, vendor control, Google services needed at boot.
PROTECTED_EXACT = {
    "com.google.android.tvlauncher", "com.google.android.launcher",
    "com.miui.home", "com.android.settings", "com.android.systemui",
    "com.android.packageinstaller", "com.android.permissioncontroller",
    "com.android.tv", "com.android.tvremote", "com.google.android.katniss",
    "com.google.android.gms", "com.google.android.gsf",
    "com.google.android.googlequicksearchbox", "com.google.android.tv.googlequicksearchbox",
    "com.google.android.leanback", "com.google.android.leanback.launcher",
    "com.google.android.tv.remote.service", "com.google.android.apps.mediashell",
    "com.android.providers.settings", "com.android.server.telecom",
    "com.android.bluetooth", "com.google.android.bluetooth",
    "com.android.inputmethod.latin", "com.google.android.inputmethod.latin",
    "com.android.externalstorage", "com.android.documentsui",
    "com.android.shell", "com.android.adb",
    "com.miui.player", "com.xiaomi.miplay",  # vendor control
}

PROTECTED_PREFIX = (
    "com.android.", "com.google.android.gms", "com.google.android.gsf",
    "com.google.android.bips", "com.google.android.tv.ads",
    "com.miui.", "com.mi.", "com.xiaomi.",
    "com.google.android.projection",  # cast
    "com.android.bluetooth", "android.",
)


def sh(serial, cmd, timeout=45):
    p = subprocess.run(["adb", "-s", serial, "shell", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return (p.stdout or "").strip()


def sh_lines(serial, cmd, timeout=60):
    out = sh(serial, cmd, timeout)
    return [l.strip() for l in out.splitlines() if l.strip()]


def getprop(serial, key):
    return sh(serial, f"getprop {key}")


def adb_state(serial):
    """get-state is a HOST-side adb command, not an Android shell builtin."""
    p = subprocess.run(["adb", "-s", serial, "get-state"],
                       capture_output=True, text=True, timeout=30)
    return (p.stdout or "").strip()


report = {
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "tvs": {},
}

for name, serial in _tvs().items():
    state = adb_state(serial)
    rec = {"serial": serial, "state": state}
    if state != "device":
        report["tvs"][name] = rec
        print(f"[{name}] NOT CONNECTED: {state}")
        continue

    rec["model"] = getprop(serial, "ro.product.model")
    rec["manufacturer"] = getprop(serial, "ro.product.manufacturer")
    rec["android"] = getprop(serial, "ro.build.version.release")
    rec["sdk"] = getprop(serial, "ro.build.version.sdk")
    rec["build"] = getprop(serial, "ro.build.display.id")
    rec["device"] = getprop(serial, "ro.product.device")

    # HOME resolver
    rec["home_resolver"] = sh(serial, "cmd package resolve-activity --brief "
                                     "-a android.intent.action.MAIN -c android.intent.category.HOME")
    # full package inventory
    all_pkgs = [l.split(":")[-1] for l in sh_lines(serial, "pm list packages")]
    rec["package_count"] = len(all_pkgs)
    pre_disabled = [l.split(":")[-1] for l in sh_lines(serial, "pm list packages -d")]
    rec["pre_disabled"] = pre_disabled
    third_party = [l.split(":")[-1] for l in sh_lines(serial, "pm list packages -3")]

    # launchable apps: MAIN + LAUNCHER + LEANBACK_LAUNCHER + HOME
    # Android 9 `cmd package query-activities` prints an ActivityInfo block per activity
    # with `packageName=<pkg>` lines; older formats use `<pkg>/<activity>` headers.
    #
    # PITFALL (cost a real miss on 2026-09-29): streaming apps on Android TV are almost never
    # exported under `android.intent.category.LAUNCHER`. Netflix, Prime Video, Globo Play,
    # Google Play Games and Google Videos register only `LEANBACK_LAUNCHER`. Querying LAUNCHER
    # alone reports them as unlaunchable, so a full parental block silently misses them and the
    # child can still open them from the Xiaomi home row. ALWAYS union LAUNCHER + LEANBACK_LAUNCHER
    # + HOME before building a block list.
    launchable = set()

    def collect(query_cmd, timeout=90):
        for line in sh_lines(serial, query_cmd, timeout):
            line = line.strip()
            if line.startswith("packageName="):
                pkg = line.split("=", 1)[1].strip()
                if pkg and pkg != "null":
                    launchable.add(pkg)
            else:
                m = re.match(r"^([a-zA-Z0-9_.]+)/(?:[\w.$]+)", line)
                if m:
                    launchable.add(m.group(1))

    for cat, tmo in (("android.intent.category.LAUNCHER", 90),
                     ("android.intent.category.LEANBACK_LAUNCHER", 90),
                     ("android.intent.category.HOME", 60)):
        collect(f"cmd package query-activities -a android.intent.action.MAIN -c {cat}", tmo)
    rec["launchable"] = sorted(launchable)

    # classify
    block, protect_also = [], []
    for pkg in all_pkgs:
        is_launchable = pkg in launchable
        is_protected = (pkg in PROTECTED_EXACT or pkg.startswith(PROTECTED_PREFIX)
                        or pkg in pre_disabled or pkg in rec["launchable"] and False)
        if is_protected:
            protect_also.append(pkg)
        elif is_launchable:
            block.append(pkg)
    rec["protected_critical"] = sorted(set(protect_also))
    rec["block_candidates"] = sorted(block)
    rec["third_party"] = third_party

    # capacity
    rec["storage"] = sh(serial, "df -h /data | tail -1")
    rec["wakefulness"] = (re.search(r"mWakefulness=(\w+)",
                                     sh(serial, "dumpsys power", timeout=40)) or [None, "?"])[1]
    fw = re.search(r"mCurrentFocus=Window\{[^}]*?\s(?:u0\s+)?([^/}\s]+)", sh(serial, "dumpsys window", timeout=40))
    rec["foreground"] = fw.group(1) if fw else "?"

    report["tvs"][name] = rec
    print(f"[{name}] {rec['model']} Android {rec['android']} | pkgs={rec['package_count']} "
          f"| launchable={len(rec['launchable'])} | block_candidates={len(rec['block_candidates'])} "
          f"| pre_disabled={len(pre_disabled)} | home={rec['home_resolver']}")

json.dump(report, open(OUT, "w"), ensure_ascii=False, indent=2)
print(f"\nwritten: {OUT}")
