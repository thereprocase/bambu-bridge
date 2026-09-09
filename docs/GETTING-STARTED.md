# Getting started with Bambu Bridge

This is the happy path: from nothing to watching your first print, in order.
Almost all of it happens in the **web app** the bridge serves — no command line
beyond the one install step, and nothing to install on your phone. If a step
fails, jump to [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) — every error the
bridge can show is listed there with a fix.

You'll need:

- A Linux host to run the bridge on (a small always-on box, a NAS, a Raspberry
  Pi, or a Home Assistant OS install — any of these is fine).
- A Bambu Lab **P1S** on the same network.
- About ten minutes.

The bridge listens on port **8080** by default. Everywhere below, replace
`<bridge-host>` with your bridge machine's address (its Tailscale name or LAN
IP) and `<printer-ip>` with your printer's IP.

The shape of the journey:

1. **Install** the bridge (one command).
2. **Open the web app** at `/app` in any browser.
3. The **first-run wizard** captures your API key and adds your printer.
4. You land on the **dashboard**.
5. **Submit your first print** — all from the web app.

---

## 1. Install the bridge

Pick the path that matches your setup. This is the only step that touches a
terminal.

### Plain Linux + systemd (recommended)

Install the package into a virtualenv, then let the included one-command
installer set up the service for you:

```bash
# from a checkout of the project, on the bridge host
python3.12 -m venv ~/bambu-bridge/.venv
~/bambu-bridge/.venv/bin/pip install .

bash deploy/install.sh
```

`deploy/install.sh` installs and starts a per-user systemd unit, so the bridge
comes back automatically after a reboot — and it generates a strong
`BRIDGE_API_KEY` for you and writes it to your config file at
`~/.config/bambu-bridge/bridge.env`. **You'll paste that key into the web app in
step 3**, so grab it now from the `BRIDGE_API_KEY=` line of that file:

```bash
grep '^BRIDGE_API_KEY=' ~/.config/bambu-bridge/bridge.env
```

If you'd rather do it by hand, or want a system-wide service and automatic
database backups, the full runbook is in [deploy/DEPLOY.md](../deploy/DEPLOY.md).
When a newer release comes out, [docs/UPDATING.md](UPDATING.md) walks you
through upgrading safely.

> Installed manually, or want to rotate the key? See
> [the API-key appendix](#appendix-set-the-api-key-by-hand) at the bottom for
> how to set `BRIDGE_API_KEY` yourself. The bridge **refuses to do anything
> until it has a key** — that's on purpose, so an unconfigured bridge is never
> wide open.

### Home Assistant OS

If you run Home Assistant OS, install the bridge as a local add-on instead —
Home Assistant hosts it for you. Follow
[homeassistant/addon/bambu-bridge/DOCS.md](../homeassistant/addon/bambu-bridge/DOCS.md),
set the API key in the add-on's **Configuration** tab, then come back here at
step 2 and open the web app.

---

## 2. Reach the bridge and open the web app

The web app, the print viewer, and the companion app all want to reach the
bridge from your phone and laptop. The recommended way is a private mesh network
like **Tailscale**:

1. Install Tailscale on the bridge host and on your phone/laptop, signed into
   the same tailnet.
2. The bridge already listens on `0.0.0.0:8080`, so it's reachable on the
   tailnet automatically.
3. (A plain LAN address works too if you don't need remote access — just use
   the host's local IP.)

Now, in any browser on that network, go to:

```
http://<bridge-host>:8080/app
```

You can also just open `http://<bridge-host>:8080/` — the bare host redirects to
`/app` for you.

You should see the **Bambu Bridge** welcome screen with a **Get started**
button. On a phone, this is a good moment to use your browser's *Add to Home
Screen* — the app then opens full-screen like a native app. There's nothing to
install from an app store; the bridge serves the whole app itself.

Tailscale is the network-layer boundary; the `BRIDGE_API_KEY` you'll enter next
is defense in depth on top of it.

---

## 3. First-run wizard — key, then printer

The first time you open the app it runs a short three-step wizard. The step rail
at the top reads **Key · Printer · Done** so you always know where you are.

### Step 1 — Connect to your bridge

The wizard asks for the bridge's address (pre-filled with the one you're
already browsing) and your **API key** — the `BRIDGE_API_KEY` from step 1.
Paste it in (there's a show/hide eye toggle so you can check it) and tap **Test
connection**.

- A green **"✓ Connected"** line means the key works — a **Continue** button
  appears. A bad key is *never* stored, so you can't accidentally save a wrong
  one and get bounced later.
- If you see **"the API key was rejected,"** double-check you copied the whole
  key.
- If you see **"This bridge has no API key configured yet,"** the bridge is
  running but `BRIDGE_API_KEY` isn't set — see
  [the API-key appendix](#appendix-set-the-api-key-by-hand).

### Step 2 — Add your printer

Before this works, the printer has to allow local connections. On the
**printer's touchscreen**:

1. Turn on **LAN-Only Mode** in the printer's network settings.
2. If your firmware offers **Developer Mode**, enable it on the printer for
   direct third-party control. Menu locations vary by model and firmware;
   use [Bambu's mode guide](https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode).
3. Read the printer's own **access code** and **IP address** from its network
   settings. This P1S release currently accepts eight numeric digits; other code
   formats and current firmware compatibility have not been validated.

LAN-only operation disables the printer's cloud connection. The bridge does not
enable Developer Mode for you, guess access codes, or provide a Bambu Cloud
connection. [Compatibility and access details](LAN-COMPATIBILITY.md).

> **The printer's IP can change over time** (DHCP). If the bridge later can't
> reach the printer, re-check the IP here first — and consider giving the
> printer a DHCP reservation on your router so it stays put.

Back in the web app, type the **IP** and the **access code** into the form. The
wizard reminds you right there where the code lives (Settings ▸ WLAN ▸ Access
Code). **You do not enter a serial number** — the bridge reads it from the
printer's TLS certificate automatically. Tap **Add printer**.

If it can't connect, the app tells you which of three things went wrong, in
plain language:

- **wrong IP, or the printer is off** — re-check the IP on the printer;
- **wrong code, or LAN-Only Mode is off** — re-check the code and that LAN-Only
  Mode is on;
- **connected but the printer went quiet** — usually transient; try again.

(If the printer is *already* registered, the app just takes you straight to it.)

### Step 3 — Done

On success the wizard shows your printer's model and friendly name and drops you
on the dashboard. The printer is live immediately and survives bridge
restarts — you won't do this wizard again unless you re-run setup or add a
second printer.

---

## 4. The dashboard

This is your home screen. At a glance you get:

- **Live status** — idle / preparing / printing / paused, and the current job.
- **Camera** — a live view, with a **3D** button that opens the 3D print viewer
  (the sliced model and toolpath) in a new tab.
- **Temperatures** — nozzle, bed, and chamber, updating live.
- **Fans** — part, aux, and chamber.
- **AMS** — the loaded filaments and which slot is active.
- **Progress** — layer and percentage *once a print is actually laying down
  plastic* (more on that below).

Everything updates over a live WebSocket, so you don't refresh. The bottom tab
bar (or left rail on a wide screen) gets you to **Printer**, **Print**, and
**Settings**.

---

## 5. Submit your first print

Tap the **Print** tab. Print submission takes a sliced **`.gcode.3mf`** file —
export one from Bambu Studio. Then:

1. **Pick** the file (the app also offers a recent-reprints list).
2. **Map AMS slots** if you're printing in color/multi-material — the app reads
   the filaments the slice expects and lets you assign each to a **physical AMS
   slot (1–4)**, matching the numbers printed on the AMS. Printing from the
   external spool? Skip the mapping.
3. **Confirm and upload.** A progress bar tracks the upload.

The bridge validates the file *before* accepting it. If something's wrong (bad
archive, a temperature out of range, or an AMS slot count that doesn't match the
slice) the app shows a specific list of issues to fix — fix the file and
resubmit.

On success you get a **"Submitted — waiting for the printer to start"** message
and land back on the dashboard. **Submitted means accepted and validated, not
yet printing.** The job then moves through `uploading → submitted → preparing →
printing` on its own; watch it advance live on the dashboard.

> **One thing that surprises people:** while the printer is heating and leveling
> the bed, the status is `preparing`, not `printing` — and there's intentionally
> **no progress percentage** yet. The percentage only appears once the first
> real layer starts. This prevents the printer from showing fake progress (and a
> possible "printing into thin air" failure) before anything is actually being
> laid down. Watch the nozzle/bed temperatures climb as the real progress signal
> during `preparing`.

That's the whole happy path. Everything below is reference and optional.

---

## What you can control & which actions confirm

Once a printer is registered, these are the everyday controls — all available
from the web app's **Controls**, and over the API. (A deeper engineering
inventory of the raw command surface is in
[docs/P1S-CONTROL-MATRIX.md](P1S-CONTROL-MATRIX.md); this is the user view.)

Each action is sent to the printer immediately. A success response (or the
button confirming in the app) means **"the bridge forwarded your command,"**
not **"the printer finished doing it."** For anything that takes time or changes
the print, watch the live dashboard to confirm it actually happened.

| You can | Endpoint | Notes |
|---|---|---|
| Pause / resume / stop a print | `POST .../print/pause`, `.../print/resume`, `.../print/stop` | Stop aborts the job. Confirm via state — "stop sent" is not "stopped" until the printer reports it. The app asks you to confirm a stop. |
| Turn the chamber light on/off | `POST .../light` | Takes effect immediately. |
| Set nozzle / bed target temps | `POST .../temperature` | Range-checked; out-of-range is rejected. Watch the temps climb to confirm. |
| Set a fan speed | `POST .../fan` | Part / aux / chamber, 0–100%. |
| Set the print speed preset | `POST .../speed` | 1 silent · 2 standard · 3 sport · 4 ludicrous. |
| Home the axes / jog the toolhead | `POST .../home`, `.../move` | Safety-gated: jog only after homing, only in safe steps, only within the build envelope. |
| AMS pause/resume/reset, change filament | `POST .../ams/control`, `.../ams/change` | Filament change confirms when the printer reports the new slot engaged. |
| Send raw G-code | `POST .../gcode` | Gated behind a deliberate confirmation in the app. For people who know exactly what they're sending. |

The actions you'll most want to **confirm by watching state** rather than
trusting the immediate response: **stop**, **filament change**, and anything
**temperature**-related — those complete asynchronously on the printer.

---

## Advanced / automation: the curl + API path

Everything the web app does, it does over the bridge's HTTP API — so you can
script the same flow from a terminal, a cron job, or another tool. You only need
this if you're automating; for everyday use, the web app above is simpler. The
full API is in [API-CONTRACT.md](API-CONTRACT.md).

`/health` and `/version` stay reachable without a key; everything else needs the
`BRIDGE_API_KEY` as a Bearer token.

### Check the bridge is alive

```bash
curl -fsS http://<bridge-host>:8080/api/v1/health      # -> {"status":"ok"}
```

### Register a printer

You don't supply a serial — the bridge reads it from the printer's TLS
certificate. You only need the IP and the access code (LAN-Only Mode on, as in
step 3):

```bash
curl -fsS -X POST http://<bridge-host>:8080/api/v1/printers \
  -H "Authorization: Bearer <BRIDGE_API_KEY>" \
  -H 'Content-Type: application/json' \
  -d '{"host":"<printer-ip>","access_code":"<8-digit code>","friendly_name":"P1S"}'
```

On success you get `201` with the printer's details, including its `printer_id`
(the serial). **Save that `printer_id`** — you'll use it in the viewer URL and
for control calls.

```json
{
  "printer_id": "01P00A3C...643",
  "serial": "01P00A3C...643",
  "model": "P1S",
  "friendly_name": "P1S",
  "connected": true,
  "first_telemetry_at": "2026-05-20T03:14:18.402Z"
}
```

If you got an error instead of `201`, the bridge tells you exactly which of the
three things went wrong (wrong IP / printer off, wrong code / LAN mode off, or
connected-but-silent) — match it to a fix in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). List registered printers with:

```bash
curl -fsS http://<bridge-host>:8080/api/v1/printers \
  -H "Authorization: Bearer <BRIDGE_API_KEY>"
```

### Submit a print

```bash
curl -fsS -X POST \
  http://<bridge-host>:8080/api/v1/printers/<printer-id>/jobs \
  -H "Authorization: Bearer <BRIDGE_API_KEY>" \
  -F 'file=@/path/to/Benchy_PLA.gcode.3mf' \
  -F 'ams_mapping=1'
```

- `file` is your sliced `.gcode.3mf`.
- `ams_mapping` is optional — a comma-separated list of **physical AMS slot
  numbers (1–4)**. Omit it to print from the external spool.

Validation and the `queued → ... → printing` lifecycle are exactly as described
in the web-app flow above; a bad file returns `422` with a list of specific
issues.

### Open the 3D viewer directly

The dashboard's **3D** button is the easy way in, but the viewer also has a
plain URL:

```
http://<bridge-host>:8080/api/v1/printers/<printer-id>/viz?token=<TOKEN>
```

- If you set `BRIDGE_VIZ_TOKEN`, use it here as `<TOKEN>`. It's **read-only** —
  safe to share, since it can't control the printer or touch files.
- If you didn't set a viz token, use your `BRIDGE_API_KEY` as `<TOKEN>`.

The viewer needs an active job to show — open it before submitting a print and
it will say there's nothing to visualize yet.

---

## Appendix: set the API key by hand

The one-command installer generates `BRIDGE_API_KEY` for you. If you installed
manually, or want to rotate the key, set it yourself. Without it, every
authenticated route returns `503 auth_not_configured`.

```bash
mkdir -p ~/.config/bambu-bridge
cp deploy/bridge.env.example ~/.config/bambu-bridge/bridge.env

# generate a strong random key and paste it after BRIDGE_API_KEY=
openssl rand -hex 32

${EDITOR:-nano} ~/.config/bambu-bridge/bridge.env
chmod 600 ~/.config/bambu-bridge/bridge.env
```

Set `BRIDGE_API_KEY=` to the value you generated. While you're in the file, you
can optionally set `BRIDGE_VIZ_TOKEN=` to a second random value — a read-only
token for sharing the 3D viewer without giving out the master key. Then restart
the bridge so it picks up the key:

```bash
systemctl --user restart bambu-bridge        # if you used deploy/install.sh
```

Now return to step 2 and open the web app.

---

## Where to go next

- Something not working? **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**.
- Moving to a newer release: **[UPDATING.md](UPDATING.md)**.
- Full deployment options, backups: **[../deploy/DEPLOY.md](../deploy/DEPLOY.md)**.
- Home Assistant: **[../homeassistant/README.md](../homeassistant/README.md)**.
- The full API: **[API-CONTRACT.md](API-CONTRACT.md)**.
