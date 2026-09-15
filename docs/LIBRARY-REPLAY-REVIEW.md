# Replay review contract

This stage analyzes immutable slices and proposed mappings. It does not send
print starts. Native receipt archival runs independently of this UI.

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
No G-code is rewritten to map spools. The eventual dispatch adapter must derive
the correct command representation, preserve start options, and be tested
separately from this review response.

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

## Evidence and remaining checks

- Synthetic tests cover sparse logical indices, explicit reordered unit/tray
  IDs, moved spools, changed RFID identity, empty slots, partial/stale inventory,
  external-only mapping, ambiguous physical pre-binds, checksums and bounded XML.
- Missing printer-model records and differing nozzle diameters are checked
  explicitly; installed hardware is never inferred from the selected slice.
- Desktop and 390 px browser checks exercised explicit tray selection and
  confirmed that every POST was a review request, with no job submission.
- Actual replay dispatch, profile/hardware confirmation, inventory recheck at
  the final dispatch boundary, Android integration and physical acceptance remain.

Source reference: official OrcaSlicer commit
`292cf0095e698a6e0f96041bd142fd41afd6ccfb`,
[tray-index mapping and presence parsing](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/GUI/DeviceCore/DevFilaSystem.cpp),
[device tray mapping](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/GUI/DeviceManager.cpp),
and [print job parameters](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/GUI/Jobs/PrintJob.cpp).
