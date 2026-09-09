# Bambu Bridge add-on

Runs the bambu-bridge server inside Home Assistant OS. It connects to your
Bambu Lab printer over the LAN and serves a translated REST + WebSocket API on
port **8080**, consumed by the Bambu Bridge HA integration and the companion
app.

## Installation

This is a local add-on (the source repo has no public git remote).

1. On the machine with the repo, vendor the bridge source into the add-on:
   ```
   homeassistant/addon/sync-bridge-source.sh
   ```
   This copies `src/`, `pyproject.toml` and `README.md` into
   `bambu-bridge/bridge/` — the Docker build context. Re-run it whenever the
   bridge source changes.
2. Copy the whole `bambu-bridge/` folder into the Home Assistant `/addons/`
   share (Samba add-on or SSH add-on).
3. **Settings → Add-ons → Add-on Store → ⋮ → Check for updates.** The add-on
   appears under *Local add-ons*. Install it (first build takes a few minutes).

## Configuration

| Option | Required | Description |
|---|---|---|
| `api_key` | yes | Bearer token for the bridge API. Use a long random string. Without it the bridge fails closed — every authenticated route returns `503`. |
| `log_level` | no | `debug` / `info` / `warning` / `error`. Default `info`. |
| `spaghetti_detection` | no | Enable the Phase-1 vision print-failure heuristic. Default `false`. |
| `ntfy_url` | no | ntfy server for push notifications. |
| `ntfy_topic` | no | ntfy topic for push notifications. |

Set `api_key`, then start the add-on.

## Networking

The add-on uses **host networking** — the bridge binds `0.0.0.0:8080`
directly on the Home Assistant host. Reach it at `http://<your-ha-host>:8080`.
Outbound printer connections (MQTT 8883, FTPS 990, camera 6000) are made by
the bridge to the printer's LAN IP, which you supply when onboarding a printer.

## 3D print viewer

The bridge also serves a self-contained **3D print-progress viewer** (the
sliced model + toolpath of the current job) in any browser at:

```
http://<your-ha-host>:8080/api/v1/printers/<printer-id>/viz?token=<token>
```

Embed that URL in a Lovelace iframe card, or open it from a phone on the same
network — the companion app reaches the same bridge the same way. Use your
`api_key` as `<token>`. (Note: there is no public git remote, so this add-on's
options cover the common cases; the read-only viewer token `BRIDGE_VIZ_TOKEN`
described in `deploy/bridge.env.example` is a deployment-level setting for
non-add-on installs.)

## Data persistence

The job database and the TOFU printer-certificate fingerprints are stored in
`/data` (the add-on's persistent volume). They survive add-on restarts and
updates. Registered printers are *not* lost on update.

## Connecting Home Assistant

After the add-on is running, install the **Bambu Bridge** integration and add
it with:

- Base URL: `http://homeassistant.local:8080`
- API key: the `api_key` set above

Then onboard your printer through the integration / companion app using its
LAN IP and 8-digit access code.
