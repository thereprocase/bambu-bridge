# Native start timing follow-up (2026-09-13)

Staged locally; not deployed during the active matching right-arm print.
Live release remains freshness-d444415. Monitoring instructions and snapshot
helper: F:/Code/bambu-job-diagnosis-20260912/MONITOR.md.

The successful receipt c8fe934dd78d40e5814bfd01a7c96739 took 34 seconds
from bridge receipt creation to dispatch and another 11 seconds to the recorded
job start. Historical data cannot separate delivery from readiness within the
34 seconds. Do not attribute this entire interval to FTPS or promise a speedup.

This change adds durable, first-observed milestone timestamps for receive,
delivery, readiness, acknowledgement, running and terminal status. The native
bridge status UI displays available stage durations and explicit MQTT diagnostic
labels. Missing historical timings remain unknown. Creation and dispatch retain
whole-second precision. Retry receipts retain first milestones: their displayed
intervals can include recovery/wait time, not just one transfer attempt.

Unchanged telemetry no longer reloads the full receipt history. Upload-only
objects skip the start-readiness probe. Real queued starts still require readiness
and at-most-once dispatch; no concurrency or automatic start replay was added.

Validation: 87 focused Python tests, 3 Node timing-display tests, Ruff checks.
The only Python warning was an existing Starlette/AnyIO deprecation.

After the print reaches a verified matching terminal state, deploy via the
existing backed-up release workflow, verify the live commit and listener health,
and inspect timings on the next user-initiated job. Do not restart during this
print or automatically send another physical start to benchmark latency.
