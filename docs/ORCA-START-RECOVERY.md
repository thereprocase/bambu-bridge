# Orca start recovery

## Orca -4030 / MQTT 3.1

Orca defines -4030 as failure publishing the local print's MQTT message. Live
reconnect traces showed `MQIsdp` protocol level 3 rejected by the old gateway,
which accepted only `MQTT` level 4. The gateway now accepts both MQTT 3.1 and
3.1.1, with the same authentication, topic restrictions and duplicate-start
suppression. Connection diagnostics retain bounded protocol-stage history,
including subscription and publish rejection reasons, without payloads/secrets.

## Finished printer readiness

Telemetry receipt time must be updated before awaiting native observers or
on-seen database persistence and before raw-bus fanout. Otherwise a fresh
pushall reply wakes the start guard while its timestamp still looks stale.
POST /api/v1/native/readiness (owner-authenticated HTTPS) forces a status-only
refresh through the same guard; it sends no print command and enqueues nothing.

IDLE, FINISH and FAILED are ready states; pressing Stop after FINISH is not
required. Quiet P1S status reports may be about 30 seconds apart. If the existing
15-second freshness check rejects an otherwise ready printer, the start path
sends a read-only pushall request, waits up to 10 seconds for a fresh print-state
report, and checks readiness again. A busy printer is still rejected. Failed
refreshes and timeouts retain distinct receipt/error codes instead of being
misreported as BBSTART_NOT_IDLE. Previously blocked jobs are not replayed;
submit a new intentional print after correcting the cause.

Orca can send a printer-local archive as `ftp://name.gcode.3mf`. Treat the
authority component as the filename when this URI has no path. It must match
the staged upload; an empty parsed path must never reserve the printer as `/`.
Staged starts rewrite both the archive URL and optional `file` field to the
immutable delivered object, preserving the inner `param` plate member.

P1S telemetry may report the inner G-code member in `gcode_file`. An exact
dispatch acknowledgement plus matching `subtask_name` can track RUNNING and
FINISH without comparing that inner member with an archive filename. A foreign
task or an unacknowledged reusable name cannot release or take ownership of a
start. No uncertain command is automatically replayed.

For a genuinely uncertain start, inspect the native inbox in the dashboard.
After verifying the printer is idle and the old command is no longer pending,
use its Resolve action. Time passing alone is not proof a physical command
failed. Preserve the receipt; do not delete the database or reset pairing.

September 12 incident: deployed PR15 revision 09810cf parsed Orca's archive URL
as `/`, bypassed the staged upload, then kept an acknowledged external start
unknown indefinitely. The exact old reservation was backed up and resolved
only after the jobs database proved that its tray print completed. The current
left-arm print was separately confirmed running; no print commands were sent.

## Printer power-cycle recovery
A confirmed active job becomes interrupted when fresh telemetry explicitly reports IDLE with empty gcode_file and subtask_name. Split identity/state deltas are supported. Missing fields, disconnection, PAUSE and named IDLE do not release ownership. The receipt and job history remain terminal; no resume, stop, or replay command is sent. Start a new print after clearing the physical bed.

## Lost-start recovery
September 17 incident: the printer acknowledged a start and was powered off
before it reported PREPARE. The receipt expired to unknown, the rebooted
printer's empty IDLE could not release it because it had never been seen
active, and every later Orca start failed with BBSTART_UNRESOLVED until the
owner resolved it by hand. The native path logged nothing, so the journal
looked healthy.

A dispatched start that was never seen active is now released by fresh
telemetry that contradicts it. Elapsed time alone still releases nothing.

- BBSTART_LOST (interrupted): at least 30 seconds after dispatch, a fresh
  report shows explicit empty IDLE. The printer restarted and holds no job.
  The grace period covers a status report that was already in flight when the
  start was published.
- BBSTART_AUTO_RESOLVED (resolved): at least 120 seconds after dispatch, a
  fresh report explicitly carries gcode_state IDLE, FINISH or FAILED. The start
  either never began or already ended. PREPARE, RUNNING, PAUSE, temperature-only
  packets and silence keep the fence.

This is safe against a late duplicate because starts are published at QoS 0 on
a clean MQTT session and are never retransmitted or replayed by the bridge. It
relies on the P1S acting on an accepted project_file within seconds rather than
holding it for minutes; if a firmware is ever observed doing that, raise
DISPATCH_EXPIRY in native_inbox.py.

A quiet idle P1S may send no state edge for a long time, so the gateway asks
for a status-only pushall when a start expires and again before it would refuse
a new start. A busy, disconnected or silent printer defers recovery; the
pushall sent on every MQTT reconnect gives the next opportunity. Transitions
are logged as native.start_state, native.start_unknown, native.start_refused
and native.start_recovery_deferred. The dashboard Resolve action remains for
anything these rules do not cover.
