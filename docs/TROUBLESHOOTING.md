# Troubleshooting Bambu Bridge

When something doesn't work, find your symptom below. Each row tells you what
the bridge thinks happened and what to do about it. The exact wording of every
error comes from `src/bambu_bridge/api/errors.py` — if you're reading a raw API
response, the `error` field is the stable name and the `remediation_hint` field
is the same advice spelled out.

Everywhere below, `<printer-ip>` is your printer's IP and `<printer-id>` is the
serial the bridge returned when you registered it.

---

## Two things to check first (they cause most problems)

These two gotchas are behind the majority of first-time failures. Check them
before anything else.

### 1. Run exactly ONE bridge per printer

A Bambu printer accepts only a **handful of simultaneous connections** (roughly
three MQTT client slots). The bridge holds one of them and shares it with every
client. If you run a **second** bridge against the same printer — for example
the Home Assistant add-on *and* a separate systemd bridge, or two copies after a
botched move — they fight over the slots and you get flapping connections,
dropped telemetry, and intermittent failures that look random.

**Fix:** make sure only one bridge process is connected to a given printer. If
you moved the bridge to a new host, stop the old one. If you run the HA add-on,
don't also run a standalone bridge against the same printer.

### 2. Enable LAN-Only Mode on the printer

The bridge connects locally, so the printer must allow local connections. On the
touchscreen: **Settings ▸ Network ▸ LAN-Only Mode → on.** If your firmware shows
a separate **Developer Mode**, enable that too. With LAN-Only Mode off, the
printer either refuses the access code or connects but goes silent — see E2 and
E3 below.

---

## When registering a printer fails

`POST /api/v1/printers` reports exactly which of three things went wrong. Match
the `error` value in the response to a row here.

| HTTP / `error` | What it means | Cause | Fix |
|---|---|---|---|
| **502 `printer_unreachable`** (E1) | The bridge couldn't even open a connection to the printer. | Wrong IP, printer powered off or asleep, or the bridge is on a different network segment (e.g. a Guest Wi-Fi). | Confirm the printer is on and the screen is awake. Re-check the IP on the printer at **Settings ▸ WLAN ▸ IP** — it can change. Make sure the bridge host and printer are on the same network (not a Guest network). |
| **403 `printer_auth_failed`** (E2) | The bridge reached the printer, but the printer rejected the access code. | Either the access code is wrong, **or** LAN-Only Mode is off (the protocol can't tell these two apart). | First, confirm **Settings ▸ Network ▸ LAN-Only Mode** is **on**. Then re-check **Settings ▸ WLAN ▸ Access Code** — it's case-sensitive and can be regenerated. Retry with the exact code shown. |
| **502 `mqtt_no_telemetry`** (E3) | The printer accepted the connection but never sent any status. | LAN-Only Mode (or Developer Mode, if your firmware has it) is off, or the local connection dropped right after connecting. | Enable **Settings ▸ Network ▸ LAN-Only Mode** (and **Developer Mode** if shown), then try again. |
| **403 `printer_cert_changed`** | The printer's security key changed since you first registered it. **This is normal right after a firmware update.** | The printer regenerates its TLS certificate on every firmware update, so the bridge no longer recognizes it (a safety check, not a real attack). | If you just updated the printer's firmware, re-trust it: `POST /api/v1/printers/<printer-id>/trust`. The bridge accepts the new key and reconnects. |
| **409 `conflict`** | A printer with this serial is already registered. | You registered it before. | Use the existing one — the error includes its `existing_printer_id`. To re-register, delete it first. |
| **422 `invalid_input`** | The request body was malformed. | Bad IP format, missing `access_code`, or you sent a `serial` field (the bridge derives the serial — don't send one). | Fix the body: `{"host": "<printer-ip>", "access_code": "<8 digits>", "friendly_name": "..."}`. |

---

## When every call returns 503

| HTTP / `error` | What it means | Fix |
|---|---|---|
| **503 `auth_not_configured`** | The bridge has no API key set, so it fails closed and refuses every authenticated route. | Set `BRIDGE_API_KEY` in your env file (`~/.config/bambu-bridge/bridge.env`), then restart the bridge. See [GETTING-STARTED.md](GETTING-STARTED.md) step 2. Note that `/health` and `/version` keep working without a key — use them to confirm the bridge is up. |
| **401 `auth_missing` / `auth_invalid`** | Your request had no `Authorization` header / `?token`, or the key was wrong. | Send `Authorization: Bearer <BRIDGE_API_KEY>` (or `?token=<key>` where headers can't be set). If you're sure the key matches the env file, restart the bridge so it re-reads the file. |

---

## When a control or print command "doesn't take effect"

A success response from a control endpoint means **the bridge forwarded your
command to the printer** — not that the printer has finished acting on it. Temp
changes, stops, and filament changes complete asynchronously on the printer.
Watch the live printer state (or the companion app) to confirm the printer
actually did it, rather than trusting the immediate HTTP response.

If a command genuinely had no effect:

- **`409 printer_offline`** — the bridge temporarily lost its link to the
  printer and is reconnecting. Wait a few seconds and try again. If it persists,
  re-check the two gotchas at the top (one bridge, LAN-Only Mode).
- **Settings change didn't apply after editing the env file** — the bridge only
  reads its environment at startup. After editing
  `~/.config/bambu-bridge/bridge.env` (changing the API key, viz token, log
  level, etc.), you **must restart the service**:
  ```bash
  systemctl --user restart bambu-bridge      # per-user unit (deploy/install.sh)
  # or, for a system-wide unit:
  sudo systemctl restart bambu-bridge
  ```
  For the Home Assistant add-on, change the value in the add-on's
  **Configuration** tab and restart the add-on.

---

## When the 3D viewer shows no job

The viewer renders the job that's **currently on the printer**. If you open it
and it says there's nothing to visualize:

- There's **no active job** — the printer is idle, or the print already
  finished. Start a print, then reopen the viewer.
- The job just started and the file hasn't been located on the printer's storage
  yet — give it a few seconds and refresh.

If the viewer says your **access token was rejected or expired**: open the link
again with a current token. If you're sharing it with `BRIDGE_VIZ_TOKEN`, make
sure that token is still set in the env file and the bridge has been restarted
since you set it. If you're not using a viz token, the viewer needs your master
`BRIDGE_API_KEY` as `?token=` instead.

---

## Useful checks

```bash
# Is the bridge process alive at all? (no key needed)
curl -fsS http://<bridge-host>:8080/api/v1/health      # -> {"status":"ok"}

# What printers does it know about?
curl -fsS http://<bridge-host>:8080/api/v1/printers \
  -H "Authorization: Bearer <BRIDGE_API_KEY>"

# Service logs (per-user unit installed by deploy/install.sh)
journalctl --user -u bambu-bridge -f

# Service logs (system-wide unit)
journalctl -u bambu-bridge -f
```

Still stuck? The logs (above) will usually name the failing phase. The
deployment runbook is in [../deploy/DEPLOY.md](../deploy/DEPLOY.md), and the
setup walkthrough is in [GETTING-STARTED.md](GETTING-STARTED.md).
