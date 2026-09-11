# Native durable inbox

Enable with `BRIDGE_NATIVE_DURABLE_INBOX=true`; the default remains false for
existing installations. This mode changes FTP completion from printer delivery
to durable server custody. It includes its own persisted delivery/start lifecycle
and a common admission hook for app, HTTP, and native file-start commands.

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
for existing, non-staged SD files use the same admission fence. Managed app jobs
reserve admission before uploading to the printer, not merely before publishing
their start. REST/resumed uploads and renaming staged files are explicitly
unsupported. Original upload names support local SIZE and RETR after custody;
their immutable printer copies are visible through the existing SD listing.

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
| BBSTART_ACCEPTED | Matching command acknowledgment received |
| BBSTART_RUNNING / BBSTART_COMPLETED | Matching fresh run / subsequent completion observed |
| BBSTART_UNKNOWN | Dispatch outcome uncertain; do not blindly retry |
| BBSTART_CANCELLED / BBSTART_NOT_IDLE | Queued start suppressed |

Sanitized receipt/status rows are exposed through the owner native status API
and the dashboard's explicit connection check. Deferred failures also emit a
native MQTT project_file failure report using uppercase `FAIL`, which Orca's
[DeviceManager handler](https://github.com/SoftFever/OrcaSlicer/blob/main/src/slic3r/GUI/DeviceManager.cpp)
recognizes. Rendering on the operator's exact installed Orca build still needs
natural-use verification; no fake physical print success is sent.
Private payloads and command bodies stay in the pairing directory's native-inbox
subdirectory, never logs or source control. Capacity is conservatively bounded
at 512 MiB / 128 retained payloads. Redundant, delivered, terminal local copies
expire after seven days; receipt metadata expires after ninety days once its
payload has been released. Pending/uncertain jobs are never automatically pruned.
Printer copies are not automatically deleted. On capacity rejection, the owner
can explicitly discard eligible local cached copies; failed partial uploads are
never presented as complete receipts.

## Safety and recovery

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
- A unique immutable file in a fresh active printer report identifies the staged
  run even if its acknowledgment was lost. Only a subsequent matching terminal
  report releases that run automatically. Idle alone never proves a start.
- External file starts receive unique bridge sequence IDs, translated back in
  native acknowledgments. They require matching acknowledgment and active-file
  evidence for automatic lifecycle completion. A completed run permits a new,
  deliberately uploaded second copy; unresolved starts block competing commands.
- No confirming evidence within 120 seconds becomes UNKNOWN, not a retry.
- The awaited printer observer does not depend on the lossy UI event bus. A
  process-exclusive file lock prevents competing inbox workers; startup fails
  closed if durable custody cannot acquire its fence. Disable/reconfigure is
  refused while uploads or start reservations remain unresolved.
- Managed jobs reserve before upload; cancellation during upload blocks dispatch.
  Job-scoped stops require current matching run evidence, so an old job cannot
  automatically stop a newer/foreign print. Explicit printer stop remains usable.
- The owner HTTPS dashboard offers cancel-held-start, explicit retry-delivery,
  resolve-after-checking-printer, and discard-local-cache actions. Except local
  queued cancellation, recovery requires fresh idle telemetry plus confirmation.
  Retrying delivery can dispatch a still-queued start; the confirmation says so.
- Direct panel/vendor actions cannot be fenced by Beluga. Foreign or uncorrelated
  telemetry never gets claimed as a staged run; ambiguous cases require review.
- This release does not bundle the larger PR13/API operation redesign or PR5 APK
  changes. No APK change is needed for native upload custody or TLS compatibility.

## Rollout and rollback

Keep the previous release and private database/config backups. Confirm service
health and unchanged printer state after a bridge restart. Verify synthetic file
custody, byte-for-byte downstream delivery, and cleanup without sending a print
command. Real print-start/second-copy hardware verification remains a natural-use
acceptance check, not a claim derived from offline tests.

Do not blindly roll back to a version that does not understand pending custody
receipts. Resolve pending starts/deliveries first or preserve the inbox for
explicit recovery; never replay a dispatch just because the bridge restarted.

## Verification

The isolated gateway test deliberately blocks printer connection establishment,
receives a successful durable FTP receipt while it is blocked, then checks
delivery-before-start and exactly one start publication. Unit tests cover disk
sync failure, capacity/size limits, early resets, late teardown, restart recovery,
duplicate generations and queued cancellation. Fixtures contain synthetic bytes
and do not issue physical printer commands.
