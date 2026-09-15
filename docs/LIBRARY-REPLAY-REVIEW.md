# Replay review contract

Review analyzes immutable slices and proposed mappings without starting a print.
A separate feature flag and confirmed Start action enable native replay. Native
receipt archival runs independently of this UI.

`POST /api/v1/library/captures/{capture_id}/replay-review` accepts:

```json
{
  "printer_id": "registered-printer",
  "choices": {"0": 3, "2": 1},
  "expected_inventory": null,
  "refresh": true
}
```

Keys are zero-based **logical filament indices**, including any gaps. Values
identify current physical trays. The example maps logical filaments 1 and 3
to slots 4 and 2 of AMS A; unused logical filament 2 needs no selection.
Suggestions do not select spools on the user's behalf. An inventory fingerprint
from the preceding response detects changed identity, material, color or presence.
Refreshing status can send `pushing.pushall`; this route never starts a print.

## Address spaces

For standard four-slot AMS units, the tray ID is `unit_id * 4 + slot_id`.
Both input IDs are zero-based and come from the report, never array order.
Current support is P1S and units 0–3. AMS HT, mixed AMS Lite and dual-nozzle
layouts use other rules and are intentionally rejected until qualified.

The P1S external spool's telemetry ID is `254`. This is a **selection ID**,
not an instruction to copy 254 into a start command. External-spool start
parameters and logical toolpath selectors are separate address spaces.
No G-code is rewritten to map spools. The dispatch adapter emits `use_ams=false`
and an all-`-1` array for an external spool. For AMS it emits `use_ams=true` with
physical tray IDs at the selected logical indices. Both arrays span the complete
saved preset count, including unused gaps and trailing presets. This follows
Orca's `get_ams_mapping_result`, not just the number of used filaments.

Material inventory freshness uses complete AMS identity/presence frames and
separate external-spool reports. A nozzle-temperature or humidity update does
not refresh it. Partial material changes invalidate the saved frame. Loss of
the MQTT session clears both inventories. A choice needs a matching material
in a present, supported tray reported within 30 seconds.

Matching color/profile identifiers suggests a candidate; it does not establish
physical spool identity, equivalent filament behavior or sufficient quantity.
Unknown registered printer models and a differing reported nozzle diameter
require review. A saved slice supplies its required printer and nozzle, not
proof of the selected printer's installed hardware.

## Confirmed start and recovery

Set `BRIDGE_LIBRARY_REPLAY_ENABLED=true` only for a qualified native gateway.
`POST /api/v1/library/captures/{capture_id}/replay` accepts the reviewed printer,
choices, `inventory_fingerprint`, required `nozzle_diameter` and `bed_type`,
`ready_confirmed: true`, an `options` object and a new 32-character lowercase
hexadecimal `id`. Options are strict booleans: `bed_leveling`, `flow_cali`,
`vibration_cali`, `layer_inspect` and `timelapse`.

The confirmation explicitly covers a clear bed, P1S hardware, nozzle, plate and
enough suitable material for the saved profiles. It can supply a missing P1S
registration model; it cannot override a conflicting model or reported nozzle.
Start defaults use the previous attempt's recorded options when available.

The server records the request ID before printer preflight. It then refreshes
inventory, validates the fixed approval, stages hash-verified bytes and queues
one native receipt with that same ID. The normal worker rechecks materials and
printer readiness immediately before claiming Start. Existing upload, publish,
acknowledgement, active-job and interruption handling remain the state authority.

`GET /api/v1/library/replays/{id}` inspects the request. Repeat POSTs with the
same body return its outcome. The same ID with different choices returns 409.
Claims survive capture deletion and backup/restore. A crash before a dispatch
receipt does not authorize an automatic retry. If a native receipt expires,
the library uses its archived attempt or reports `receipt_unavailable`; it does
not mislabel an old dispatched job as safely unstarted.

The dashboard persists the complete approval locally before POST. After a lost
response it checks the saved ID. A 404 offers an explicitly confirmed retry of
that exact request, never a newly generated ID. A queued or uncertain result
does not offer another Start. A known terminal result permits a fresh review.

## Evidence and remaining checks

- Synthetic tests cover sparse logical indices, explicit reordered unit/tray
  IDs, moved spools, changed RFID identity, empty slots, partial/stale inventory,
  external-only mapping, ambiguous physical pre-binds, checksums and bounded XML.
- Missing printer-model records and differing nozzle diameters are checked
  explicitly; installed hardware is never inferred from the selected slice.
- Tests use the real durable inbox and native worker with simulated transport:
  concurrent/lost-response retries produce one Start, bytes remain unchanged,
  changed materials/options or busy hardware block final dispatch, and transfer
  failures/restarts release unsent reservations without automatically retrying.
- Desktop and 390 px browser checks exercised explicit confirmation, invalidated
  choices, lost requests, lost accepted responses and reload. Two network attempts
  retained one identical approval and created one durable native receipt. The
  browser fixture never connects to a printer.
- Android integration and physical replay acceptance remain unqualified.

Source reference: official OrcaSlicer commit
`292cf0095e698a6e0f96041bd142fd41afd6ccfb`,
[tray-index mapping and presence parsing](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/GUI/DeviceCore/DevFilaSystem.cpp),
[device tray mapping](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/GUI/DeviceManager.cpp),
[full preset mapping serialization](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/GUI/SelectMachine.cpp),
and [print job parameters](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/GUI/Jobs/PrintJob.cpp).
