# Spool inventory beta — install, upgrade, disable and rollback

Bridge 0.7.0b1 with Android app 0.21.0-beta.9 (version code 59). The app updates the 0.20.26
(code 50) and later beta builds in place.

## What this build turns on

| Feature | State in this build |
|---|---|
| Spoolman-backed spools: create, select, weigh, archive, numbers on labels | On when configured |
| Assigning spools to AMS slots and the external holder, moves, restarts | On when configured |
| Legacy cabinet import, explicit link and reviewed per-spool rollback | On when configured |
| Stock Orca material selection per product | On when configured; affects only the display below |
| Virtual spool display in Orca through the native gateway | Off. Cannot be enabled on this build |
| Usage estimates and review | On when configured |
| Automatic consumption deduction | Off unless `BRIDGE_INVENTORY_AUTO_DEDUCT=true`; uncertain prints are held for review |
| Cross-colour refill: preview, saved intent, readback review | On when configured |
| Cross-colour refill: physical declaration writes and restoration | Off. Cannot be enabled on this build |
| Auto-refill history and fault journal (from 0.20.26) | Unchanged; now included in state backups |
| Preview cache age limit, native snapshot count limit | On: 30 days, newest 10 (see Retention) |

"Configured" means `BRIDGE_SPOOLMAN_URL` and `BRIDGE_INVENTORY_DIR` are set. Without
them the bridge behaves as the 5f60bf7 build and the app keeps its existing Filament
screen.

## Target combination

- Spoolman v0.22.1 (commit `18882400442d5ea114d064a14c8708d3b8ab4352`).
- Orca Slicer v2.4.2 official Linux AppImage, network plug-in 02.03.00.62.
- Android API 36 emulator (test-signed builds) and the owner's Pixel 9 Pro.
- Python 3.13 in the bridge LXC, reusing the 5f60bf7 release environment; this build
  adds no runtime dependency.

## Before installing

1. Run Spoolman v0.22.1 on the private network. Do not expose it publicly. If it needs a
   token, put it in `BRIDGE_SPOOLMAN_BEARER` on the bridge only; the app never sees it.
2. Back up the bridge state, and Spoolman with it when Spoolman runs on SQLite on the
   same host:

   ```sh
   BRIDGE_DB_PATH=/var/lib/bambu-bridge/jobs.db \
   SPOOLMAN_DB_PATH=/var/lib/spoolman/spoolman.db \
       deploy/state-backup.sh
   ```

   Add `BRIDGE_INVENTORY_DIR` once it exists (not on the first install). Keep the
   archive name; rollback uses it. The archive also holds the pairing records and the
   per-printer diagnostic history.

## Install on the bridge LXC (release directory)

The LXC runs each build from its own directory under `/opt/bambu-bridge-releases` and
selects it with a systemd drop-in. This build needs no new Python packages, so it reuses
the running release's environment.

1. Check the bundle: `sha256sum -c SHA256SUMS`.
2. Unpack the sdist into a new release directory and link the running environment:

   ```sh
   R=/opt/bambu-bridge-releases/spool-beta-<commit>
   mkdir "$R" && tar -xzf bambu_bridge-0.7.0b1.tar.gz -C "$R" --strip-components=1
   ln -s "$(readlink -f /opt/bambu-bridge-releases/<running>/.venv)" "$R/.venv"
   ```

3. Activate it. `release-activate.sh` imports the release with its own interpreter
   before touching the unit, writes the single managed drop-in `zz-release.conf`
   (it sorts after every older `99z…` drop-in), restarts and waits for `/health`:

   ```sh
   deploy/release-activate.sh "$R" \
       --env BRIDGE_SPOOLMAN_URL=http://<spoolman-host>:7912 \
       --env BRIDGE_INVENTORY_DIR=/var/lib/bambu-bridge/inventory
   ```

   The `--env` lines live in the managed drop-in, so a rollback removes them with the
   release. The service user must be able to create `BRIDGE_INVENTORY_DIR`.

4. Check `GET /api/v1/version` reports `0.7.0b1` and `GET /api/v1/inventory` reports
   `configured: true`, a `provider_id` and `accounting_mode: disabled`.

From now on, add `BRIDGE_INVENTORY_DIR` (and `SPOOLMAN_DB_PATH` where it applies) to
every `state-backup.sh` run. The older `deploy/backup.sh` covers only `jobs.db`.

A pip-managed install (not the LXC) can still use
`deploy/bambu-bridge-update.sh --wheel bambu_bridge-0.7.0b1-py3-none-any.whl` with the
two settings in its environment file.

## Install the app

Install `bambu-bridge-0.21.0-beta.2-arm64.apk` over the existing app. It is signed with
the same key as 0.20.26 (certificate SHA-256 `9506816c…502da6`) and has a higher version
code, so Android updates it in place and keeps pairing and settings:

```sh
adb install -r bambu-bridge-0.21.0-beta.2-arm64.apk
```

Then open Filament → Open spool inventory. The Slots card at the top lists each AMS slot
and the external holder; "Put a spool here" offers spools of the slot's material.

## How assignments behave

- A slot keeps its spool while the printer reports the same material there (material,
  SKU and, for Bambu RFID spools, the tag). Colour does not count, so deliberate
  cross-colour refill aliases do not move a spool.
- Choosing another spool of the same material for a slot replaces the assignment.
- When the AMS reports the slot unloaded, or a different material, the bridge ends the
  assignment within a few seconds. Assign again after reloading.
- The bridge reads the slot itself when you assign; there is no time limit between
  opening the list and tapping.

## Disable

Remove `BRIDGE_SPOOLMAN_URL` (on the LXC: activate the same release again without that
`--env`) and restart. Inventory endpoints stop, the app shows its existing Filament
screen, and the inventory directory stays on disk untouched. Re-adding the setting
resumes from the same state. Migrated cabinet spools stay read-only while inventory is
off.

## Roll back

Bridge on the LXC, when no cabinet spool has been migrated:

```sh
deploy/release-activate.sh --rollback
```

This restores the previous `zz-release.conf`, or removes it when there was none, which
hands control back to the older drop-ins (the 5f60bf7 + 300 C release). The inventory
directory is left in place and is ignored by the older build. To also return bridge state
to the pre-upgrade backup, stop the service, run
`deploy/state-restore.sh bridge-state-<pre-upgrade>.tar.gz` (it refuses while the bridge
answers and moves replaced files into `pre-restore-<time>` directories), then start it.

After migrating any cabinet spool: first use the app's reviewed rollback for every
migrated spool (Cabinet transfers and rollback), so Spoolman ownership is archived before
the cabinet quantity becomes writable again. Only then roll back. Never run the older
build on state that still holds migrated spools: it ignores the read-only marker and
would create a second writable quantity.

Spoolman: restore its database only together with the matching bridge archive, with
both stopped, starting Spoolman first. The bridge detects an older provider state and
holds inventory writes for review.

App: Android does not downgrade in place. Uninstall, install 0.20.26, and pair again.
Bridge-side inventory data is not affected.

## Retention

- Preview caches (print sources, parsed geometry, spinner frames) unread for
  `BRIDGE_PREVIEW_MAX_AGE_DAYS` (30) are removed hourly; they rebuild on demand. Size
  caps per cache were already in place.
- Native-inbox snapshots in `pairing/native-backups`: newest `BRIDGE_NATIVE_BACKUPS_KEEP`
  (10) are kept.
- Release directories and deploy snapshots: `deploy/release-prune.py` reports what can
  go. It keeps every release the unit, its environment file or the newest drop-ins name,
  the newest `--keep` releases, and everything those reach through `.venv` links,
  `pyvenv.cfg`, `.pth` files and shebangs. It deletes only with `--apply`.
  `jobs-*` and `bridge-state-*` archives keep their own retention.
- The older `99z…` drop-ins stay until the owner chooses to collapse them; with
  `zz-release.conf` in place they no longer decide which release runs.

## Known limitations

- Stock Orca draws an unknown quantity as a full spool, and when any tray in an AMS
  reports an unknown quantity it draws every tray in that AMS as full, including
  genuine RFID spools. The app shows the actual known/unknown state. (Applies once the
  display is enabled in a later build.)
- Sync from AMS replaces a manually chosen Repro preset with the stock preset; choose the
  Repro preset again after Sync. Background updates and tab switches keep it.
- After the bridge restarts, a connected Linux Orca 2.4.2 re-authenticates but does not
  resubscribe and shows "Failed to connect to the printer". Restart Orca, or reselect
  the printer, to recover.
- In the virtual spool's material dialog, Confirm and Reset are answered locally as
  read-only; change pressure-advance K with the display off or on the printer.
- A swap of two untagged spools with the same material while the bridge is offline (or
  restarting) cannot be detected: the printer reports nothing different. Reassign in
  the app after such a swap.
- With `BRIDGE_INVENTORY_AUTO_DEDUCT=true` the bridge deducts the slicer's estimate for a
  completed single-spool print. When the AMS auto-refilled during the print, the spool
  that ran out is set to zero (or skipped if already archived) and the refill spool is
  charged the print's feed from the middle of the layer the printer reported at the
  switch to the end. This needs the refill slot to have had a spool of the same material
  assigned when the print started; the refill load and purge are not charged. It holds
  a print for review instead when the bridge restarted or lost the printer during it, a
  refill cannot be placed by layer, the external holder was used, or the print did not
  complete; a hold that starts with `partly_charged` means some spools were already
  charged. The estimate is the slicer's, not a measurement; weigh a spool to correct it.
- The external holder is not shown as a virtual spool.
- AMS remaining percentages from the printer are observations, not inventory mass.
- Cross-colour refill physical writes and automatic consumption stay off until their
  physical gates pass on the owner's printer.
