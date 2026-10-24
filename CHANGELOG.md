# Changes

This repository consolidates the bridge and Android app into a shared beta
source tree. Earlier development commits are summarized as milestone snapshots.

Current work includes durable print-start recovery, queue review tools,
connection/status reporting, model-aware feature availability, and shared
Gridline branding. Hardware qualification and further bug fixes remain active.

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
