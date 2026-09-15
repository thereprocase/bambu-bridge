# Print library: implementation checkpoint

Status: development foundation, disabled by default. This is not the completed
automatic Print capture and replay feature.

## Implemented and checked

- Immutable version-1 capture manifests distinguish exact slices, editable
  projects, original input bytes and derived previews. Hash verification is
  separate from the currently unproven project-reopening claim.
- Scoped HTTPS uploads can resume after a disconnected response or restart.
  Files are deduplicated by SHA-256. Finalization verifies every declared file;
  downloads require authentication and verify bytes again.
- The archive has no printer-command dependency. Retrying an archive transfer
  cannot submit a print. Execution records are a separate layer, linked by
  receipt UUID and slice hash, with immutable original mapping choices.
- Explicit deletion retains a capture-ID tombstone. Garbage collection removes
  only blobs without remaining references. Quotas reserve pending uploads too.
- A library-only backup preserves SQLite, committed blobs and partial-upload
  prefixes. Restore writes a new directory and verifies its contents.
- The dashboard's Library page lists artifacts, provides authenticated
  downloads, displays incomplete uploads and searches loaded results. Pagination
  handles equal timestamps without omitting entries.
- A standalone Orca plugin contains a durable local outbox and a native Library
  page. The plugin uses HTTPS Tailscale DNS names, scoped slicer credentials and
  refuses redirects. It receives only explicitly supplied artifact paths.
- When enabled, an independent worker copies retained native Orca print uploads
  into the permanent library and observes their durable execution receipts.
  Native receipts supply queued, submitted, accepted, active and terminal
  outcomes. `active` does not prove extrusion: native custody combines
  preparation, running and paused states. Historical polling records observations,
  not a claim that every intermediate transition was seen.
- Receipt revisions prevent a slower observer from replacing a newer outcome.
  Restarted scans are idempotent, deleted capture IDs stay deleted, and an archive
  error never repeats a printer dispatch. Only native uploads with an exact
  `project_file` plate reference are ingested. Bridge-managed/external receipts
  without those bytes are not guessed into a capture by name or timestamp.
- The dashboard exposes print attempts and a **read-only replay mapping review**,
  plus an independently enabled, explicitly confirmed Start action.
  Analysis verifies the selected plate checksum, logical filament handshakes,
  nozzle, bed and filament settings with bounded ZIP/XML reads. Spool choices
  use explicit AMS IDs; unused logical filaments need no choice. Temperature
  heartbeats cannot refresh old inventory. Partial identity updates invalidate
  the inventory until a complete report arrives, and reconnect clears it.

Replay review does not submit a print or create a print attempt. `mapping_complete`
means the proposed mapping passed software checks; physical hardware, filament
profile and material quantity still require review. `dispatch_available` defaults
to false. The authenticated review may request MQTT `pushing.pushall` to refresh
status, but never sends a printer start or control command.

Confirmed replay uses the existing native inbox, delivery worker and receipt
reconciliation. It preserves the exact slice bytes and records each execution
against the original capture. A durable request ID fixes the approved mapping,
start options, slice hash and inventory snapshot. The dashboard retains that
request across navigation and lost responses. Reusing its ID only retrieves its
outcome; it cannot submit another print. Failed or interrupted staging releases
an unsent reservation; an ambiguous dispatched start remains fenced for review.

The official Windows nightly from 2026-09-14 loaded the Library and diagnostic
page capabilities successfully. The diagnostic page executed against an empty
bed. Model capture, host-side networking, project round-trip recovery and normal
Print interception are **not** qualified by that loading test. The installed
stable OrcaSlicer 2.4.2 predates the Python plugin system.

## Enable a development bridge

Set `BRIDGE_LIBRARY_DIR` to a private persistent directory owned by the bridge
service. `BRIDGE_LIBRARY_QUOTA_BYTES` defaults to 20 GiB. Back up that directory
separately from the bridge job database and credentials. Unset the directory
setting to disable the library without deleting its contents.

The quota is conservative: it reserves each artifact's declared bytes for each
capture, even when physical content is deduplicated. The local plugin queue is
bounded to 4 GiB, including delivered copies. There is no automatic retention
policy or deletion of promised original files. Local queue management UI and
multi-process quota coordination remain release work.

No deployed service configuration or active printer is changed by this commit.
Adding the directory setting requires the normal controlled service rollout.

`BRIDGE_LIBRARY_REPLAY_ENABLED=true` additionally enables confirmed native P1S
replays. It defaults to false. Keep it disabled until qualification on the target
printer. Starts require a fresh material report and clear-bed, hardware and
material confirmation; inventory and readiness are checked again at the final
native dispatch boundary. Changing the inventory, disabling the library, losing
the original capture or changing approved options blocks that queued replay.
The configured `BRIDGE_MAX_TRANSFER_BYTES` limit also applies to archived starts.

## API contract

All archive mutations are independent of printing:

| Route | Purpose | Authentication |
| --- | --- | --- |
| `POST /orca/{printer}/library/captures` | Create or resume an immutable manifest | Scoped Orca key, HTTPS |
| `GET /orca/{printer}/library/captures` | List this client's captures | Same |
| `GET .../captures/{id}` | Read committed offsets | Same |
| `PUT .../captures/{id}/files/{name}?offset=N` | Append at most 4 MiB | Same |
| `POST .../captures/{id}/finalize` | Verify all files and commit | Same |
| `GET /api/v1/library/captures` | Browse, with `limit`, `before`, `before_id` | Owner or paired device |
| `GET .../captures/{id}/files/{name}` | Download verified bytes | Owner or paired device |
| `GET /api/v1/library/usage` | Reserved and stored bytes | Owner or paired device |
| `GET /api/v1/library/history-status` | Native observer availability and last error | Owner or paired device |
| `GET .../captures/{id}/attempts/{attempt}/events` | Durable attempt observations | Owner or paired device |
| `POST .../captures/{id}/replay-review` | Read-only slice and current mapping analysis | Owner or paired device |
| `POST .../captures/{id}/replay` | Explicitly confirmed, idempotent native Start | Owner or paired device |
| `GET /api/v1/library/replays/{id}` | Inspect the saved request and native receipt | Owner or paired device |
| `DELETE .../captures/{id}` | Explicit capture deletion | Owner |
| `POST /api/v1/library/collect-unreferenced` | Explicit blob collection | Owner |
| `POST /api/v1/library/verify` | Database and blob integrity scan | Owner |

Slicer credentials cannot download the owner's archive or browse another
client's captures. A revoked or wrong-printer key is rejected. Keys are never
put in download URLs. The archive stores artifact basenames, not source paths.

## Backup and recovery

Run as the service user, using the bridge's Python environment:

```sh
python -m bambu_bridge.library_backup backup /var/lib/bambu-bridge/library /var/lib/bambu-bridge/backups/library-YYYYMMDD
python -m bambu_bridge.library_backup restore /var/lib/bambu-bridge/backups/library-YYYYMMDD /var/lib/bambu-bridge/library-restored
```

Destinations must not exist. Failed operations retain an `INCOMPLETE` marker
and are not eligible restore sources. Do not activate an incomplete restore.
Backup holds the archive write lock while copying; it does not lock the printer
job database. Schedule larger backups when archive ingestion can wait.

After a successful restore, change `BRIDGE_LIBRARY_DIR` in a controlled rollout
to the restored path. Keep the original directory for rollback. Printer pairing
and credential backups remain a separate operational task.

## Remaining integration gates

1. **Orca artifact APIs:** obtain a read-only full-project snapshot and an
   exact outgoing-payload callback sharing an immutable project revision. Add
   opt-in import-time original preservation. See
   [the generic upstream proposal](ORCA-ARTIFACT-HOOK-RFC.md).
2. **Capture qualification:** populated plates, unsaved projects, painting,
   modifiers, selected plates, canceled sends, concurrent windows and upgrade
   compatibility. A mesh export must not masquerade as an editable project.
3. **Execution linkage:** native upload receipts are linked by UUID/hash. Extend
   that explicit link to plugin captures and managed API jobs; native uploads
   without a start request are not yet catalogued as attempts.
4. **Preview:** derive printable surfaces from the captured project; retain the
   current toolpath fallback until matching and geometry are proven.
5. **Replay:** review, confirmation, native dispatch and durable request recovery
   are implemented and tested with simulated printers. Qualify physical moved-spool
   replay, external-spool replay and power-cycle recovery in an agreed test window.
6. **Distribution:** supported stock build, pairing UI, queue/retention controls,
   Android library integration and physical acceptance in an agreed test window.

## Klipper / Moonraker decision

Keep Bambu control in the current bridge. Klipper firmware is a separate printer
conversion and does not provide slicer project capture. Moonraker's printer
control API expects Klippy, so running it beside this bridge does not remove the
Orca or AMS gaps. The library should not acquire a second printer-state owner.

Use Moonraker's history model as a reference: distinct job IDs, outcomes,
timestamps, metadata and filament usage. Preserve the archive as the authority
for source/project/slice bytes. Each replay will be a new execution record with
its own inventory snapshot and material mapping.

Leave a small printer-adapter boundary for capabilities, current material
inventory, upload, start and status. Implement a Moonraker client adapter when
a real Klipper printer needs it; defer a compatibility facade until a specific
client requires one.

Sources: [Klipper installation](https://www.klipper3d.org/Installation.html),
[Moonraker printer API](https://moonraker.readthedocs.io/en/latest/external_api/printer/),
[history API](https://moonraker.readthedocs.io/en/latest/external_api/history/),
[file API](https://moonraker.readthedocs.io/en/latest/external_api/file_manager/).
