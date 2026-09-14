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
  cannot submit a print. Printer execution records will be a separate layer.
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
3. **Execution linkage:** bind capture IDs and content hashes to concrete print
   attempts, including uploads canceled before dispatch. Do not join by name or
   approximate timestamps.
4. **Preview:** derive printable surfaces from the captured project; retain the
   current toolpath fallback until matching and geometry are proven.
5. **Replay:** fresh logical-filament-to-AMS mapping, hardware/material preflight,
   one attempt per confirmed start and no automatic resend of ambiguous starts.
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
