# Reliable starts — implementation and release gate

Status: draft; not deployed or hardware-verified.

## Contract

`POST /api/v1/printers/{printer}/start-operations` accepts
`operation_id`, `file_name`, and optional physical `ams_mapping`.
It returns an operation promptly, before downloading/validating/staging the
card file. `GET /api/v1/start-operations/{id}` reads that same operation;
`GET /api/v1/printers/{printer}/start-operation` reads the current reservation.

An operation identity is retained. Identical inputs replay the existing result;
changed inputs conflict. A new copy needs a new identity. A unique SQLite index
allows one reservation per printer. Queue claim, job creation, and queue
consumption commit together. Legacy upload/queue/native starts share admission,
but native and legacy upload clients do not supply durable intent identities:
their delayed retries after a completed print cannot be deduplicated reliably.

States are `accepted`, `validating`, `staging`, `dispatching`,
`awaiting_observation`, `observed_started`, and terminal/unknown states.
`submitted` in the legacy job API means dispatch attempted, not printer ACK.
Neither MQTT publication nor HTTP success proves physical execution.

A transport/observation timeout retains ownership as `outcome_unknown`.
Restart cancels pre-dispatch operations; dispatch/active operations become
unknown. Neither path automatically resends a physical command.

Owner-key-only `POST /api/v1/start-operations/{id}/resolve` requires the current
`revision` and `confirm_printer_idle: true`. The owner must inspect the printer.
The server quiesces the local worker and requires fresh connected idle telemetry
before atomically releasing ownership. It never sends a stop during resolution.
The old job becomes canceled **tracking**, with `tracking_resolved_unknown`;
this is not evidence that the physical print failed or was canceled.

Native `project_file` requests use managed validation and preserve supported
command fields, AMS indices, and sequence. The current validator supports the
root `file:///sdcard/*.gcode.3mf` form and `Metadata/plate_1.gcode` only.
Unsupported options/plates are rejected, not rewritten. Raw `gcode_file` starts
and unknown print commands are rejected. Raw G-code is limited to the existing
typed motion/thermal grammar; SD starts, macros, line-number tricks, and unknown
instructions fail closed. This intentionally narrows advanced passthrough and
needs desktop compatibility verification.

## Effects and constraints

- File/AMS/temperature validation remains in place. Existing card files still
  download and upload; skipping that is a separate optimization release.
- Validation runs off the event loop. Reservation transactions contain no
  network I/O. Printer idle/freshness is rechecked at the publish boundary.
- Local session identities and event timestamps reject old queued events.
  Disconnect invalidates session ownership; an old worker cannot stop a newer
  observed session. Stop timeouts do not count as confirmed cancellation.
- Correlation currently uses a fresh start edge, name, and local session epoch.
  It is not a firmware-issued globally unique job identity. Simultaneous panel
  starts and missed/coalesced events still need explicit fault/hardware tests.
- Unknown operations deliberately block another start. A timeout never expires
  that protection. Terminal identities survive printer/history removal; active
  ownership cannot be cascaded away.
- HA alerts continue to consume physical printer telemetry. Operation timeout
  does not emit a synthetic physical print failure.
- Paired credentials can submit/read; resolving unknown requires the owner key.
  Operation identities are not authorization secrets.

## Required before release

1. Verify raw G-code restrictions and native failure reporting with the actual
   desktop client (including unsupported options and delayed legacy retries).
2. Complete missed/coalesced-event and restart reconciliation tests. Test
   external panel starts during staging/publication and same-file repeats.
3. Test the paired APK across tab changes, background/resume, process death,
   printer/bridge switching, dropped POST response, and stale/out-of-order GETs.
   No hidden-screen polling, camera, or reconnect regression is acceptable.
4. Verify operation retention/migration and owner-resolution UX on a DB copy.
5. When idle, back up DB/config; deploy server first, then matching APK.
   Never restart the service during an active print. Hardware acceptance must
   explicitly cover a first copy and a deliberate second copy without duplicates.

Do not roll back to an old server while a reservation is unresolved: the old
version does not understand ownership. Reconcile at idle, preserve the DB and
operation records, and coordinate server/app rollback rather than blindly
restoring an older history snapshot.

The current worker/recovery implementation assumes one server process per DB,
as in the deployed systemd service. Multi-process executor/recovery fencing is
not implemented; do not configure multiple workers against the same database.
