# Orca start recovery

## Finished printer readiness

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
