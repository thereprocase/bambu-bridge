# Deploying Bambu Bridge

This is the full deployment runbook. If you just want the quickest path from
nothing to a running bridge, use [docs/GETTING-STARTED.md](../docs/GETTING-STARTED.md)
instead — it points back here for the details.

The target is **plain Linux with systemd**. The bridge connects to the printer
with `CERT_NONE` for the printer's rotating self-signed cert, so **it needs no
CA file at runtime**. Provisioning is four moves: install the code, set an API
key, run it as a service, register the printer over the API.

Throughout, replace these placeholders with your own values:

| Placeholder | Meaning |
|---|---|
| `$USER` | the Linux user the bridge runs as (your login user is fine) |
| `<your-host>` | the bridge machine's address (LAN IP or tailnet name) |
| `<repo-path>` | where you put a checkout of the project source |
| `<printer-ip>` | your printer's LAN IP (from its touchscreen) |

The bridge listens on port **8080** by default.

## Prereqs (one-time)

* `python3.12`, `sqlite3`, `openssl`, `rsync`, `git` available on the host.
* A user account (`$USER`) that can run a systemd unit. The bridge binds
  `:8080` (>1024), so it needs no special privileges or capabilities.
* For a **per-user** service (the easy path below), the user should have
  lingering enabled if you want it to run without an active login session:
  `sudo loginctl enable-linger $USER`.

## 1. Install the code

Two equivalent ways to get the source onto the host — pick one.

**(a) From a git remote** (once one is configured):

```bash
git clone <remote-url> ~/bambu-bridge
cd ~/bambu-bridge
```

**(b) From a local working copy** — run this from inside your source checkout:

```bash
mkdir -p ~/bambu-bridge
rsync -a --delete \
    --exclude .git --exclude .venv --exclude .local --exclude '*.jsonl' \
    ./ ~/bambu-bridge/
cd ~/bambu-bridge
```

Then, in either case, install the package into a virtualenv:

```bash
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install .   # installs the `bambu-bridge` console script
```

**Reproducible installs (recommended):** if `uv.lock` is present, use uv to pin
the exact versions:

```bash
uv sync --frozen
```

## 2. Configure

```bash
mkdir -p ~/.config/bambu-bridge ~/.local/share/bambu-bridge
cp deploy/bridge.env.example ~/.config/bambu-bridge/bridge.env
openssl rand -hex 32          # paste into BRIDGE_API_KEY=
${EDITOR:-nano} ~/.config/bambu-bridge/bridge.env
chmod 600 ~/.config/bambu-bridge/bridge.env
```

At minimum, set `BRIDGE_API_KEY`. The bridge **fails closed without it** —
every authenticated route returns `503` until a key is set. The annotated
template ([bridge.env.example](bridge.env.example)) documents the rest;
the optional `BRIDGE_VIZ_TOKEN` (a read-only token for sharing the 3D viewer
without the master key) is worth setting now if you plan to embed the viewer in
Home Assistant or open it from a phone.

> **Dev `.env` vs production `bridge.env`.** During development the bridge reads
> a `.env` in the project root. In a deployment it reads
> `~/.config/bambu-bridge/bridge.env`. Same variable names, different file — the
> production env file fully replaces the dev `.env`, so you don't keep a `.env`
> on a server.

## 3. Run as a service

### The easy path: `deploy/install.sh` (per-user unit)

```bash
bash deploy/install.sh
```

This reads the unit template, substitutes `$USER` into it, installs it to
`~/.config/systemd/user/bambu-bridge.service`, and enables + starts it. After
this:

```bash
systemctl --user status bambu-bridge
journalctl --user -u bambu-bridge -f          # structured JSON logs
curl -fsS http://localhost:8080/api/v1/health # -> {"status":"ok"}
```

To survive a reboot without an interactive login, enable lingering for the user
(see Prereqs): `sudo loginctl enable-linger $USER`.

### The manual path: a system-wide unit

If you prefer a system service (starts at boot, independent of any login
session), substitute your user into the template and install it under
`/etc/systemd/system/`. The template uses the placeholder
`BRIDGE_USER_PLACEHOLDER` for both `User=` and `Group=`:

```bash
sed "s/BRIDGE_USER_PLACEHOLDER/$USER/g" deploy/bambu-bridge.service \
  | sudo tee /etc/systemd/system/bambu-bridge.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now bambu-bridge.service

sudo systemctl status bambu-bridge
journalctl -u bambu-bridge -f
curl -fsS http://localhost:8080/api/v1/health
```

`enable --now` means start it right now AND on every boot. The unit reads
`~/.config/bambu-bridge/` and writes `~/.local/share/bambu-bridge/` under the
chosen user's home; storage paths use systemd's `%h` home specifier so the same
unit is portable across users.

## 4. Reach it over the network

The bridge binds `0.0.0.0:8080`, so any device that can route to `<your-host>`
on port 8080 can use it.

The recommended remote-access boundary is a private mesh network like
**Tailscale**: install it on the bridge host and on your phone/laptop on the
same tailnet, then reach the bridge at `http://<your-host>:8080` using the
host's tailnet name. Tailscale is the network-layer auth boundary; the
`BRIDGE_API_KEY` Bearer token is defense in depth on every `/api/v1/*` call
(except `/health` and `/version`, which stay open).

A plain LAN IP works too if you don't need off-network access.

## 5. Add the printer

No restart, no env — register at runtime. The body is just
`{host, access_code, friendly_name?}`; the bridge derives the serial from the
printer's TLS cert automatically (don't send a `serial`).

```bash
curl -fsS -X POST http://localhost:8080/api/v1/printers \
  -H "Authorization: Bearer <BRIDGE_API_KEY>" \
  -H 'Content-Type: application/json' \
  -d '{"host":"<printer-ip>","access_code":"<8-digit code>","friendly_name":"P1S"}'
```

The IP and the 8-digit LAN access code both come from the printer's touchscreen
(turn on **Settings ▸ Network ▸ LAN-Only Mode** first). If the call fails, the
bridge distinguishes three causes for you — E1 unreachable (502, wrong IP or
printer off), E2 auth (403, bad access code or LAN-Only Mode off), E3 silent
(502, Developer Mode off / link dropped). See
[docs/TROUBLESHOOTING.md](../docs/TROUBLESHOOTING.md) for the fix table.

## Backups / restore

The repo ships a daily online-backup timer. It runs `deploy/backup.sh`, which
uses SQLite's `.backup` API (consistent on a live WAL database — no need to stop
the service) and keeps the newest gzipped copies in
`~/.local/share/bambu-bridge/backups`.

The backup unit files (`deploy/bambu-bridge-backup.service` and `.timer`) are
system-unit templates with `User=`/`Group=` set to a default user — edit those
to your `$USER` before installing them alongside a system-wide bridge unit:

```bash
sudo cp deploy/bambu-bridge-backup.service \
        deploy/bambu-bridge-backup.timer  /etc/systemd/system/
# edit User=/Group= in the .service to match $USER
sudo systemctl daemon-reload
sudo systemctl enable --now bambu-bridge-backup.timer
```

Restore from a backup:

```bash
sudo systemctl stop bambu-bridge     # (or: systemctl --user stop bambu-bridge)
gunzip -c ~/.local/share/bambu-bridge/backups/jobs-<stamp>.db.gz \
  > ~/.local/share/bambu-bridge/jobs.db
sudo systemctl start bambu-bridge
```

## Updating

```bash
# from inside an updated source checkout
rsync -a --delete \
    --exclude .git --exclude .venv --exclude .local --exclude '*.jsonl' \
    <repo-path>/ ~/bambu-bridge/
cd ~/bambu-bridge
.venv/bin/pip install .
systemctl --user restart bambu-bridge      # or: sudo systemctl restart bambu-bridge
```

**An update only takes effect after you restart the service** — the bridge reads
its code and environment at startup. Registered printers and the job database
live under `~/.local/share/bambu-bridge/` and survive the update.

## Optional: WSL2 / Windows host

You can run the bridge inside WSL2 on a Windows machine. The systemd unit works
unchanged; the only extra wrinkle is keeping WSL2 itself running across Windows
reboots, since WSL2 doesn't start on its own.

* Ensure `systemd=true` is set in `/etc/wsl.conf` inside the distro.
* Install and configure the bridge exactly as above.
* To bring WSL2 up automatically, add a one-time Windows Task Scheduler entry
  that starts the distro headless at logon. In an **admin PowerShell on
  Windows** (adjust the distro name and user):

  ```powershell
  $action  = New-ScheduledTaskAction  -Execute 'wsl.exe' `
              -Argument '-d Ubuntu -u <your-user> -- /bin/true'
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
  $settings= New-ScheduledTaskSettingsSet -StartWhenAvailable
  Register-ScheduledTask -TaskName 'WSL2 boot Ubuntu' `
      -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited
  ```

  After this, every Windows login starts WSL2, which starts the bridge. If you
  need it before any user logs in, `-AtStartup` with `-User "SYSTEM"` works too,
  with the usual auto-login tradeoffs — pick what fits your machine.

This is an optional convenience for Windows users; plain Linux + systemd is the
primary, simplest target.
