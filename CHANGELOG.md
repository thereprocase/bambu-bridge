# Changes

This repository consolidates the bridge and Android app into a shared beta
source tree. Earlier development commits are summarized as milestone snapshots.

Current work includes durable print-start recovery, queue review tools,
connection/status reporting, model-aware feature availability, and shared
Gridline branding. Hardware qualification and further bug fixes remain active.

## Unreleased

### Bridge

- Lift the control gate for home, jog, extrude, bed/vibration calibration, AMS
  change and unload on the P1S: they match OrcaSlicer's open DeviceManager
  builders or are plain G-code the P1S already runs. Home, jog, extrude and
  calibration wait for an idle printer, as in Orca. AMS change and unload now
  use Orca's wire form (`ams_change_filament` with `ams_id`/`slot_id`; unload is
  target and slot 255).
- Report "Not available on P1S" for xcam modules and the nozzle accessory
  command, per Orca's `C12.json` (`support_ai_monitoring`,
  `enable_set_nozzle_info` both false). The AMS drying cycle, M84 and
  skip-objects (the P1S reports no `fun` flags) stay withheld.
- Prune the bridge's own job files from the printer's storage (hourly, idle
  only, keeping the current file, in-flight uploads and the newest three
  settled ones); a refused root listing is a 503, never an empty card.
- Give every slot material with a stock Generic preset its tray_info_idx
  (PCTG = GFG97), so Orca's AMS sync lists it.

### Android app 0.21.0-beta.13 (code 63)

- Movement, load/unload and calibration controls enable on a P1S.

## 0.7.0b1 - 2026-10-22 (spool inventory beta build; not published)

Built on the camera warm-stream line (5f60bf7) that the bridge LXC runs.

### Spool inventory (requires `BRIDGE_SPOOLMAN_URL` and `BRIDGE_INVENTORY_DIR`)

- Add authenticated Spoolman-backed inventory (pinned Spoolman v0.22.1): spool, product
  and vendor create, weigh and archive with durable operation tokens; an uncertain
  provider write is held for reconciliation and never retried automatically.
- Add physical source assignments with revisions, source confirmation epochs and a
  persistent provider identity that detects a replaced or restored inventory.
- Add legacy cabinet UUID import, explicit link and reviewed rollback with one writable
  quantity owner.
- Add stock Orca material selections per product from a pinned catalogue (Orca v2.4.2
  BBL identities) and a downstream-only virtual spool display for the native gateway.
  The display is off and cannot be enabled on this build.
- Add usage shadow accounting from canonical pre-dispatch manifests. Automatic
  consumption is off.
- Add cross-colour refill alias ownership: preview, saved intent, explicit execution,
  readback adoption, external-change release and idle restoration. Physical writes are
  gated off on this build.
- Keep calibration writes from a virtual spool's material dialog local to the gateway.
- Add `deploy/state-backup.sh` and `deploy/state-restore.sh` covering jobs, inventory,
  refill ownership, pairing and native-inbox records, with an optional Spoolman snapshot.

### Carried from the running deployment

- P1S sliced-job nozzle limit 300 C (owner-approved 2026-10-17); bed limit stays
  100 C. Fractional `S` values are parsed so 300.1 cannot truncate to 300.
