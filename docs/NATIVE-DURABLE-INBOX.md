# Native durable inbox — implementation preview, NOT a production rollout

`BRIDGE_NATIVE_DURABLE_INBOX=false` remains the default. Do not enable this
preview on a live printer yet. It changes the meaning of FTP completion from
printer delivery to durable server custody and therefore requires an integrated
start-operation lifecycle before release.

## Implemented boundary

The authenticated Orca upload path has no printer connection, PWD or STOR
dependency. Incoming data is drained in bounded chunks into private server
storage. Blocking disk writes run outside the event loop. Before replying
`226 BBFTP_STORED id=<opaque-id> bytes=<count>`, Beluga fsyncs the payload and
directory and commits its receipt in a FULL-synchronous SQLite transaction.
No new phone polling or wake lock is involved.

A separate ordered worker uploads immutable, server-generated printer paths.
Matching native `project_file` commands are persisted and held until printer
delivery completes; their URL is rewritten to that immutable object. Commands
for existing, non-staged SD files keep their previous behavior. REST/resumed
uploads are explicitly unsupported in this preview.

Receipt, delivery and start are different facts:

| State/code | Meaning |
| --- | --- |
| BBFTP_STORED | Complete upload durably saved on Beluga; not on printer yet |
| BBFTP_STORAGE_UNAVAILABLE / BBFTP_STORAGE_WRITE_FAILED | No successful custody receipt |
| BBFTP_SIZE_LIMIT / BBFTP_RECEIVE_FAILED | Upload not accepted as complete |
| BBFTP_BUSY | Receiving capacity unavailable; explicit rejection, not indefinite wait |
| BBDELIVERY_OK | Printer FTP confirmed the file transfer |
| BBDELIVERY_FAILED / BBDELIVERY_INTERRUPTED | Delivery failed or requires review; no automatic replay |
| BBSTART_SENT | Command publication returned; NOT proof of a physical start |
| BBSTART_UNKNOWN | Dispatch outcome uncertain; do not blindly retry |
| BBSTART_CANCELLED / BBSTART_NOT_IDLE | Queued start suppressed |

Sanitized receipt/status rows are exposed through the owner native status API
and the dashboard's explicit connection check. Deferred failures also emit a
native MQTT project_file failure report; actual Orca rendering is NOT verified.
Private payloads and command bodies stay in the pairing directory's native-inbox
subdirectory, never logs or source control. Capacity is conservatively bounded
at 512 MiB / 128 records; there is deliberately no automatic deletion yet.

## Safety behavior and remaining release blockers

- A reset before protocol EOF stays fatal. A reset/broken pipe after the TLS
  protocol already delivered EOF no longer erases previously buffered bytes.
  Unit tests distinguish these cases. A local blocked-printer fixture exposed
  the late BrokenPipe failure; this does not prove all live Orca resets share it.
- Expanding the old-curl matrix to durable custody reproduced curl 18 / FTP451
  with an incoming TLS1.3 ConnectionResetError on two runs. The passive data TLS
  context now issues zero post-handshake session tickets; the old-client matrix
  then passed both TLS versions and both custody modes (48 synthetic uploads).
  Control, camera, MQTT and phone TLS contexts are unchanged. TLS1.3 stays enabled.
  This is a reproduced compatibility fix, not yet live Orca verification.
- Starts are claimed transactionally before publication and never automatically
  replayed after uncertain dispatch. Interrupted delivery is marked for review.
- Native stop/pause cancels queued starts, serialized against native dispatch.
- Concurrent unresolved starts are refused. **A sent start currently remains
  unresolved indefinitely**: physical-session correlation, completion and
  deliberate second-copy admission must integrate with the reliable-start work
  (PR13) before production use. An idle snapshot alone is not sufficient proof.
- Cross-process worker ownership/fencing and coherent admission with other
  HTTP/app start entry points remain required. Do not run two preview workers.
- Add operator reconciliation and explicit retention/cleanup before rollout.
  Never clear an ambiguous start just to admit the next job.
- Verify Orca's real command URL/sequence identity, saved-card browsing and
  failure rendering. Same-name generations are immutable; ambiguous duplicate
  commands are refused rather than attached to a newer upload.
- No throughput benchmark or successful live Orca acceptance has been claimed.
  No APK update or printer restart was performed for this preview.

## Verification

The isolated gateway test deliberately blocks printer connection establishment,
receives a successful durable FTP receipt while it is blocked, then checks
delivery-before-start and exactly one start publication. Unit tests cover disk
sync failure, capacity/size limits, early resets, late teardown, restart recovery,
duplicate generations and queued cancellation. Fixtures contain synthetic bytes
and do not issue physical printer commands.
