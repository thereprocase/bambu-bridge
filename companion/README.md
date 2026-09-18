# Bridge Library Companion — Windows preview

A separate desktop application for stock OrcaSlicer. Orca slices; the companion
keeps explicitly selected original files and saved projects, receives exact
sliced uploads, and sends reviewed captures to the bridge's permanent library.
The Orca Python plugin is optional. No Orca fork or upstream hook is required
for this workflow.

This is an **archive preview**, not the completed automatic Print integration.
It does not render meshes, launch Orca, watch imports, snapshot unsaved edits,
or start a printer. The interface requires a saved project and explicit input
association. Installer, persistent credential pairing and normal-Orca upload
qualification remain release work.

## Run

Extract the entire Windows ZIP and open `BridgeLibraryCompanion.exe`. Keep the
`_internal` directory beside it. Python is included. This development build is
unsigned. App data is stored separately in `%LOCALAPPDATA%/BridgeLibraryCompanion`.
Changing or replacing the app folder does not delete that data.

From a source checkout with Python 3.12 and Tcl/Tk:

```powershell
python -m pip install -r companion/requirements.txt
python companion/companion.py
```

In the standalone bundle's `source` folder, run `python companion.py` instead.
The same `library_plugin.py` archive client is included in that folder.

1. Enter your bridge's **HTTPS Tailscale DNS address**, printer ID, and an
   **upload-only scoped Orca key** from bridge Settings → OrcaSlicer. The key is
   held in memory; it is not saved to the archive or app settings. Deployment
   tooling may prefill `COMPANION_BRIDGE_URL`, `COMPANION_PRINTER_ID`, and
   `COMPANION_UPLOAD_KEY` in the process environment. Never use the owner key.
2. Use **Add original files** as soon as you choose your inputs. This copies
   their bytes immediately; deleting or changing those source paths later
   cannot change the preserved copies. Select only files you want archived.
3. Prepare your plate in Orca and **save its full project as `.3mf`**. Use
   **Add saved Orca project** after the final edits. A slice renamed to `.3mf`
   is insufficient. Selecting a file later does not capture its import-time
   version, and selecting a project does not capture subsequent unsaved edits.
4. Add the exact `.gcode.3mf` with **Add sliced file**, or receive it from
   Orca using the upload connection below. Select its actual plate index.
5. Select the corresponding saved inputs (Ctrl-click for multiple), review
   the title/version, and confirm that they belong to this slice. Independently
   declare whether the selected originals include every imported file.
6. Click **Save selected capture to bridge**. Interrupted network transfers
   remain in the local outbox; **Retry pending archives** resumes them.
   **Open bridge library** shows the saved artifacts and existing replay review.

Without selected inputs, this creates an explicitly incomplete slice-only
capture. Receiving an upload never guesses its project by filename, timestamps,
or whichever Orca window is active. Repeated uploads receive distinct local
receipts; repeating an already frozen capture uses its existing archive ID.
Storage verification does not claim that a project has passed a reslice test.

## Receive uploads from stock Orca through OctoPrint compatibility

The companion implements `GET /api/version` and multipart
`POST /api/files/local`, including Orca's optional `plateindex` field. This is
the upload subset of the protocol, not a full OctoPrint server or printer driver.
Neither a real OctoPrint server nor this endpoint can obtain source CAD that
Orca never includes in its request.

The receiver is off by default. Start it with your **desktop's** HTTPS Tailscale
Serve address (the bridge's existing address continues to serve the bridge):

```powershell
BridgeLibraryCompanion.exe --public-origin https://your-desktop.your-tailnet.ts.net
```

An existing Tailscale Serve HTTPS route must forward to `http://127.0.0.1:8787`
on this desktop. The loopback hop is private; Orca is offered only HTTPS and
certificate verification stays enabled. The app does not modify Tailscale,
Funnel, firewall rules, or Orca profiles. Inspect existing Serve routes before
assigning a route; do not replace another application's endpoint.

Create a separate Orca printer preset with **Use 3rd-party print host** enabled.
Choose **Octo/Klipper**, enter the companion's HTTPS Host address, and copy the
companion key using the app's button. This key is distinct from the bridge
upload key. Click Orca's **Test**, then use **Upload**, with the upload folder
empty. The selected plate must actually be present inside the received archive.

**Upload and print returns an explicit error.** It is never quietly downgraded
to an archive success. Starting prints is a separate, qualified bridge workflow
with current filament mapping and explicit confirmation. The older bridge
OctoPrint adapter's fixed AMS mapping is not reused for companion starts.

Switching this preset to Octo/Klipper changes Orca's native Bambu device/AMS
experience; use the bridge dashboard for those controls. Retain the existing
native preset while this new handoff is being qualified.

## Durability and limits

- One companion owns each data directory; a second instance is rejected.
- Inputs, received slices, and frozen outbox copies are separate. All count
  toward a conservative 4 GiB local quota, including delivered copies. A
  capture is limited to 2 GiB, 64 artifacts, and 512 MiB per artifact.
- SHA-256 checks protect local snapshots, uploads, retries and downloads.
  Interrupted writes without a committed receipt are not shown as received.
- Per-upload receipt IDs and an outbox journal survive restart; changing an
  already frozen association is rejected. No archive retry starts a print.
- No automatic expiry or cleanup is performed. Local retention management and
  signed installer/update distribution remain release gates. Back up the app
  data directory and the bridge library independently.
- The listener accepts authenticated requests only through its configured
  HTTPS origin. It binds loopback, checks the proxy hop, refuses foreign browser
  origins and URL tokens, bounds streamed multipart bodies, and accepts one
  in-flight upload. Local credential files belong to the current desktop user.

## Build and verify

Use an isolated Windows Python 3.12 environment with `requirements.txt` and
`pyinstaller==6.22.3`, and PowerShell 7. The build preserves existing output directories:

```powershell
./companion/build-windows.ps1 -Python /path/to/python.exe -OutputDirectory /new/build/path
```

The ZIP includes source and file hashes. Protocol fixtures exercise Orca's
request shape, deleted originals, lost upload responses, restart, association
confirmation, capture-journal recovery, duplicate sends, invalid paths, quotas,
wrong plates, authentication, and refusal to start. These checks do not claim
that a populated project has round-tripped through Orca or that its real upload
dialog has been qualified on the user's profile.

An opt-in packaged diagnostic creates a hidden window and synthetic loopback
upload, refuses a print request, checks the inbox and persistent queue, and
exits. It requires a fresh data directory and sends no external requests:

```powershell
BridgeLibraryCompanion.exe --data-dir /new/diagnostic/data --smoke-report /path/report.json
```

Design and remaining integration work: [Desktop companion decision](../docs/COMPANION.md)
(in the source repository).
