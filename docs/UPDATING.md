# Updating Bambu Bridge

This document covers every update path: the host script, the Home Assistant
add-on, and what the browser app's "Check for updates" panel actually does.

**The short version:** updating the bridge is a three-step operation on the
bridge host — pull or copy new code, reinstall the package, restart the
service. Config and job history are preserved across every update.

---

## What is preserved across an update

Before anything else: **you will not lose data**.

| Location | What it holds | Survives update? |
|---|---|---|
| `~/.config/bambu-bridge/bridge.env` | API key, optional viz token | Yes — never touched |
| `~/.local/share/bambu-bridge/jobs.db` | Full job history | Yes — the script backs it up before touching anything |
| `~/.local/share/bambu-bridge/backups/` | Rolling database backups | Yes |
| `~/bambu-bridge/.venv/` | Python virtualenv | Upgraded in place by pip |

The registered printer list lives inside `jobs.db` and survives with it. You
do not need to re-register your printer after an update.

---

## Path 1 — Host update script (plain Linux / WSL2)

The `deploy/bambu-bridge-update.sh` script handles the full sequence safely.
Run it from inside the source checkout (or copy it to the host alongside the
installation):

```bash
bash deploy/bambu-bridge-update.sh
```

What the script does, in order:

1. **Records the current version** — calls `GET /api/v1/version` so it can
   print a before/after comparison at the end.
2. **Backs up the database** — uses SQLite's Online Backup API (`sqlite3 .backup`),
   which is consistent on a live WAL database with no need to stop the service
   first. The backup lands in `~/.local/share/bambu-bridge/backups/` as
   `jobs-pre-update-<timestamp>.db.gz`. If `sqlite3` is not available it falls
   back to a plain `cp`. The script will not proceed past this step if the
   backup fails.
3. **Updates the code** — three sub-cases:
   - If the installation is a **git checkout**: `git pull --ff-only`, then
     `pip install .`.
   - If you supply a **pre-built wheel** with `--wheel /path/to/file.whl`:
     `pip install /path/to/file.whl`.
   - Otherwise the script prints an rsync command for you to run and exits.
4. **Restarts the service** — detects whether the unit is a per-user unit
   (`systemctl --user`) or a system unit and restarts it. System units use
   `sudo systemctl restart`.
5. **Waits for the bridge to come back** — polls `GET /api/v1/health` every
   two seconds for up to 30 seconds and reports when it responds.
6. **Prints old vs new version** — so you can confirm the update landed.

### Options

| Flag | Meaning |
|---|---|
| `--prefix DIR` | Installation directory (default: `~/bambu-bridge`) |
| `--wheel PATH` | Install a specific wheel instead of pulling from git |
| `--no-restart` | Update code but skip the service restart |
| `--help` | Print usage and exit |

### Rsync-based update (non-git installation)

If the installation is not a git checkout, copy the updated source in first:

```bash
rsync -a --delete \
    --exclude .git --exclude .venv --exclude .local --exclude '*.jsonl' \
    <repo-path>/ ~/bambu-bridge/
bash ~/bambu-bridge/deploy/bambu-bridge-update.sh
```

The rsync excludes the virtualenv and any local data files; the script then
reinstalls into the existing virtualenv and restarts the service.

### Manual steps (if you prefer not to use the script)

```bash
# 1. Back up the database (online — service can stay up)
sqlite3 ~/.local/share/bambu-bridge/jobs.db \
    ".backup '$HOME/.local/share/bambu-bridge/backups/jobs-manual-$(date -u +%Y%m%dT%H%M%SZ).db'"

# 2. Pull updated code (git checkout)
git -C ~/bambu-bridge pull --ff-only

# 3. Reinstall
~/bambu-bridge/.venv/bin/pip install ~/bambu-bridge

# 4. Restart (per-user unit — adjust for system unit if applicable)
systemctl --user restart bambu-bridge

# 5. Confirm
curl -fsS http://localhost:8080/api/v1/version
curl -fsS http://localhost:8080/api/v1/health
```

---

## Path 2 — Home Assistant add-on

The Bambu Bridge add-on is built inside HA Supervisor from the
`homeassistant/addon/bambu-bridge/` directory. The bridge source is
**vendored** into that directory (see `RELEASING.md §4`) because the dev repo
has no public remote.

To update the add-on:

1. In the dev repo, re-sync the vendored source:

   ```bash
   make addon-vendor
   ```

   This runs `homeassistant/addon/sync-bridge-source.sh`, which copies `src/`
   and `pyproject.toml` into the add-on build context.

2. The new version string in `homeassistant/addon/bambu-bridge/config.yaml`
   is what HA Supervisor displays in **Settings → Add-ons → Bambu Bridge**.

3. Trigger a rebuild in the Supervisor UI:
   **Settings → Add-ons → Bambu Bridge → Rebuild**

   The Dockerfile installs the vendored source into the container's virtualenv
   during the build, so no separate pip step is needed on the host.

Add-on config and the job database are stored in HA's persistent data volume
and survive rebuilds unchanged.

To check whether the vendored copy is stale without re-syncing:

```bash
make addon-vendor-check
```

This exits non-zero and lists any `src/` files newer than their vendored
counterpart.

---

## Path 3 — Browser "Check for updates" panel

The browser app's Settings screen includes an "About / version" section with a
**Check for updates** button. It is honest about what a browser tab can and
cannot do.

**What it shows:**

- Current bridge version (from `GET /api/v1/version` — live, not cached).
- The SPA's own build constant (the JS version baked into `main.js`).
- If a release manifest URL is configured in Settings, a best-effort check
  against that manifest. If the manifest is unreachable (the bridge may be
  intentionally offline), it says so plainly — it does not error loudly.

**What it does when an update is available:**

The panel shows the available version and the exact runbook to run on the
bridge host, with a copy-to-clipboard button:

```
cd ~/bambu-bridge && git pull   # or: rsync per docs/UPDATING.md
.venv/bin/pip install .
systemctl --user restart bambu-bridge
```

A one-liner note follows: "After the bridge restarts, this page will reconnect
automatically."

**What it does NOT do:**

The browser never runs a privileged install. It cannot `ssh` into the bridge
host, it cannot `sudo`, and it does not pretend otherwise. Surfacing the exact
command and making the post-restart reconnect seamless is the responsible web
answer.

**Post-restart reconnect:**

When the WS connection drops (because the service is restarting), the app
shows "Bridge restarting..." and reconnects with exponential backoff. On
reconnect it re-reads `/api/v1/version` and, if the version changed, toasts
"Updated to v{X}."

**Update manifest URL:**

By default the manifest check is **off** — a strict appliance deployment may
have no outbound internet, and a failed network call should not pollute the
Settings screen. An operator can fill in a manifest URL in Settings to enable
the remote check. The manifest is fetched client-side (no proxy through the
bridge) and expected to return JSON with at least a `"version"` field.

---

## Rollback

If an update causes a problem, restore the pre-update database backup and
reinstall the previous version:

```bash
# Stop the service
systemctl --user stop bambu-bridge
# (or: sudo systemctl stop bambu-bridge)

# Restore the database from the pre-update backup
gunzip -c ~/.local/share/bambu-bridge/backups/jobs-pre-update-<stamp>.db.gz \
    > ~/.local/share/bambu-bridge/jobs.db

# Install the previous wheel or check out the previous tag
~/bambu-bridge/.venv/bin/pip install /path/to/previous-version.whl

# Restart
systemctl --user start bambu-bridge
```

The rolling backups in `~/.local/share/bambu-bridge/backups/` (managed by
`deploy/backup.sh` and its timer) give you additional recovery points beyond
the pre-update snapshot.
