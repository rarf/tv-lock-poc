# TV Lock — proof of concept

Parental control for Android/Google TV over ADB, driven from Home Assistant.

> **This is a proof of concept, not a product.** It works on the TVs it was
> tested on (Xiaomi MiTV-MSSP3 and MIBOX3, Android 9) and it has no reason to
> work on your TV until someone has looked at it. See *Honest limitations*.

## What it does

- **Blocks every app** on a TV with a single reversible command
  (`pm disable-user`), never uninstalling anything.
- **Refuses to lock a TV that someone is watching.** The service checks the
  TV's `media_session` and, when available, the Jellyfin session, and returns
  the title that is playing instead of cutting it.
- **Self-expiring unlock.** An unlock opens a time window; when it lapses a
  watchdog re-locks automatically.
- **Keeps a journal.** Every package the tool disabled is recorded, so unlock
  re-enables exactly what the tool disabled and nothing else.
- **Never touches a protected package** (launcher, settings, ADB, Play Store,
  input, casting, accessibility…). Blocking those would brick the TV.

## Install

```bash
git clone https://github.com/rarf/tv-lock-poc
cd tv-lock-poc

# 1. Tell it which TVs exist
cp tools/config.example.json tools/state/config.json
$EDITOR tools/state/config.json      # {"tv_sala": "192.168.1.10:5555"}

# 2. Inventory the TV (read-only) before changing anything
python3 tools/tv_inventory.py

# 3. Generate the API token the service uses
mkdir -p tools/state
python3 -c "import secrets,pathlib; p=pathlib.Path('tools/state/api_token'); \
p.write_text(secrets.token_urlsafe(32)); p.chmod(0o600)"

# 4. Run the service
python3 tools/tv_lock_service.py --host 127.0.0.1 --port 8129
```

Wire it into Home Assistant with the files in `lovelace-tv/`, and run
`tools/tv_lock_publish.py` so the dashboard shows the real state.

## Honest limitations

Read this before you trust it with a child.

- **It is deterrence, not security.** ADB is open on port 5555 on most TVs.
  Anyone who can reach the device on the LAN can re-enable everything. Real
  enforcement needs network isolation (VLAN/firewall) or a closed ADB port.
- **Android 9, single user profile.** Locking is per-app, not per-profile.
  There is no per-child account, because these TVs have none.
- **A blocked app is not blocked content.** The app stops opening; the
  password inside Netflix is not touched.
- **HDMI is not covered.** A PlayStation on HDMI is hardware. The
  `tv_hdmi_guard.py` helper can put the panel to sleep if the source is
  switched while locked, but anyone with the remote can wake it.
- **Firmware updates can change package names**, and a vendor update may
  re-enable packages the tool disabled.
- **Only the TVs it was tested on.** Android 9 Xiaomi units. Other brands have
  different launchers, different input handling, and sometimes a locked ADB.

## How it was built

The code started as a script for one household and was generalised: the TV
list, the block list, the protected-package set and the state all come from
configuration. `tools/discovery.py` can find ADB endpoints on the local subnet
and identify a TV.

The Home Assistant integration is plain YAML (`rest_command` + `script` +
`template`), not a custom component, so it can be installed by copying files.

## Licence

MIT. See `LICENSE`.
