# Home Assistant support for bambu-bridge

Two pieces, both in this directory:

| Piece | Path | What it is |
|---|---|---|
| **Integration** | `integration/custom_components/bambu_bridge/` | A Home Assistant custom integration that consumes the bridge's `/api/v1` REST + WebSocket API and exposes the printer as HA entities. |
| **Add-on** | `addon/` | A Home Assistant Supervisor add-on that runs the bridge itself, packaged for HA OS. |

## Architecture — one bridge

The bridge owns the single LAN connection to the printer (MQTT/TLS/FTPS). The
HA integration is a *client* of the bridge — it never speaks the printer
protocol. This matters: a Bambu printer has a limited number of MQTT client
slots, so there must be exactly **one** bridge. The add-on *is* that bridge
when Home Assistant runs on the release host; the integration points at it.

```
  printer ──MQTT/TLS/FTPS──> bridge (add-on) ──REST/WS──> integration ──> HA entities
                                   └────────────────────> companion app
```

The integration needs **no bridge protocol changes** — it maps the existing
translated snapshot (`docs/API-CONTRACT.md` §6, `src/bambu_bridge/translate.py`)
straight onto HA entities.

The same bridge also serves a self-contained **3D print-progress viewer** (the
sliced model + toolpath for the current job) at
`http://<your-ha-host>:8080/api/v1/printers/<printer-id>/viz?token=<BRIDGE_VIZ_TOKEN>`.
You can embed that URL in a Lovelace iframe card, or open it from a phone over
the network — the read-only `BRIDGE_VIZ_TOKEN` lets you share viewer links
without handing out the master API key. The companion phone app reaches the same
bridge over the network as well; the integration and the app are both just
clients of the one bridge.

## Install

### 1. The add-on (the bridge)

The supplied add-on is installed as a *local* add-on from this public source
repository. It is not currently distributed through an add-on store repository.

1. On the dev box, vendor the bridge source into the add-on build context:
   ```
   homeassistant/addon/sync-bridge-source.sh
   ```
2. Copy `homeassistant/addon/bambu-bridge/` into the Home Assistant
   `/addons/` share (via the Samba or SSH add-on).
3. In HA: **Settings → Add-ons → Add-on Store → ⋮ → Check for updates** —
   "Bambu Bridge" appears under *Local add-ons*. Install it.
4. In the add-on **Configuration** tab, set `api_key` to a long random string.
5. Start the add-on. It now serves `http://<your-ha-host>:8080`.

See `addon/bambu-bridge/DOCS.md` for the full option reference.

### 2. The integration

1. Copy `integration/custom_components/bambu_bridge/` into your Home Assistant
   config directory at `custom_components/bambu_bridge/` — or run
   `integration/install.sh <path-to-ha-config>`.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Bambu Bridge.**
4. Enter:
   - **Base URL** — `http://homeassistant.local:8080` when the add-on runs on
     the same HA host, or `http://<bridge-host>:8080` when the bridge runs
     elsewhere (e.g. the dev box).
   - **API key** — the same `api_key` you set on the add-on.

Each printer the bridge knows becomes one HA device with sensors, a camera,
controls (pause/resume/stop/home, chamber light, target temps, fans, print
speed) and connectivity/problem binary sensors. Bridge `event` frames are
re-fired on the HA event bus as `bambu_bridge_event` for automations.

## Future bridge-side niceties (not required, not blocking)

- `GET /api/v1/server` — would feed HA device firmware/hostname (today the
  integration best-effort reads `_raw.info`).
- `GET /camera/stream.mjpeg` — upgrades the camera from snapshot-poll to a
  live stream.
- A clean speed-level readback — today the speed `select` derives the current
  level from `_raw.spd_lvl` and is otherwise write-mostly.
