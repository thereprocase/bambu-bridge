> For native P1S status, per-print AMS/external selection and camera, use
> [native P1S mode](NATIVE-P1S.md). This page describes the older upload adapter.

# OrcaSlicer → Bambu Bridge → printer

The bridge includes an **Octo/Klipper print-host adapter for stock OrcaSlicer
2.4.2**. Your desktop sends a sliced `.gcode.3mf` over HTTPS to the bridge;
the bridge uploads it to the printer over FTPS and, if explicitly requested
and permitted, starts it through the existing validated MQTT job lifecycle.
The printer access code stays on the server. No proprietary networking DLL
is distributed or replaced.

This is a server integration, not a native Orca Python plugin. Orca's newer
Python Printer Agent workflow is still work in progress. Native AMS sync and
Orca's Bambu device controls are not provided; use the bridge dashboard for
camera, real-time 3D, controls and filament selection.

## Connect

1. Open the bridge dashboard over **HTTPS with a certificate your desktop
   trusts**, and sign in with the owner key. A Tailscale Serve address works
   at home and away when the desktop is on the same tailnet. The Android
   app's pinned, self-signed direct address is not interchangeable with a
   browser/Orca-trusted HTTPS address. Keep certificate verification enabled.
2. Go to **Settings → OrcaSlicer**. Select your printer and name this desktop.
   Start with **Upload only**. Click **Create Orca connection** and copy the
   Host URL, API key and Device UI URL. The key is shown only once.
3. In Orca, edit your Bambu printer preset. Enable **Advanced → Basic
   information → Use 3rd-party print host**, and save a separate Bridge
   preset so your direct connection remains available.
4. Open the printer connection / Wi-Fi dialog. Choose **Octo/Klipper** and
   paste the Host URL and API key. Click **Test**. Put the dashboard address
   in **Device UI**. This key is for the slicer, not dashboard login.
5. Slice **one plate at position 1**. Use **Upload** and leave the upload
   folder empty. The bridge preserves the slice bytes and adds a short
   unique suffix to the SD card filename. Upload does not start anything;
   select the file later on the printer’s SD card screen to print it.

The desktop needs network access only to the bridge. It does not need
direct access to the printer's MQTT, FTPS or camera ports. Bridge local
pairing must be enabled (`BRIDGE_PAIRING_DIR`) to store slicer credentials.
Behind a reverse proxy, configure `BRIDGE_TRUSTED_PROXIES` for only the
actual proxy; see [local pairing](LOCAL-PAIRING.md).

## Upload and print

Create a connection with **fixed AMS mapping** or **single external spool**
permission to use Orca's **Upload and print** action. Mapping entries are
zero-based physical slots in slicer filament order: `1` maps a one-filament
slice to physical slot 2; `0,2` maps its two filaments to physical slots 1
and 3. It is not automatic AMS discovery. Check the loaded materials before
printing and create a new connection when the mapping changes.

Upload-only keys return an explicit error for a print request. A successful
upload-and-print request means the bridge **queued** the validated job;
follow its actual upload, preparation and printing state in the dashboard.
The bridge retains its normal temperature, checksum and filament checks,
watchdog, and print defaults (textured plate, bed leveling and vibration
calibration enabled). Use the dashboard if you need different preparation
options. Busy/offline printers are rejected before an Orca print is submitted.

## Preview limits and credentials

- Only a single sliced plate at position 1 is accepted. Other plate indices,
  multi-plate archives, plain G-code and unsliced projects receive errors.
  The bridge never silently prints plate 1 for a different selection.
- This is the upload subset of the OctoPrint protocol, not a full OctoPrint
  implementation. Native Bambu device/AMS features are separate.
- Each key is scoped to one printer and this adapter. It cannot administer
  the bridge or access the main API. Keys are hashed at rest, HTTPS-only,
  and revocable individually in **Settings → OrcaSlicer**. Revoking one
  blocks subsequent requests; already accepted prints continue.
- Orca stores the key in its connection settings. Treat exported presets
  containing it as private; revoke a key if an export was shared. Never
  paste the bridge owner key or printer access code into this connection.
- The first integration release is protocol- and mock-printer-tested.
  A physical Orca-to-printer print remains an acceptance check for a user
  selected plate; release automation does not start production prints.

## Wire contract

Host URL: `https://bridge.example/orca/PRINTER_ID` (no `/api/v1` suffix).

| Request | Purpose |
| --- | --- |
| `GET /orca/{printer}/api/version` | Orca connection test; `X-Api-Key` required |
| `POST /orca/{printer}/api/files/local` | Multipart `file`, `print`, `path`, `plateindex`; scoped key required |
| `GET /api/v1/orca/clients` | List connections without secrets; owner over HTTPS |
| `POST /api/v1/orca/clients` | Create key once; name, printer_id, optional ams_mapping |
| `DELETE /api/v1/orca/clients/{id}` | Revoke key; owner over HTTPS |

For creation, `ams_mapping: null` means upload only, `[]` means one external
spool, and a nonempty array means fixed AMS slots. Tokens in URL query
parameters, owner keys and phone tokens are not accepted by the adapter.

Protocol reference: [Orca 2.4.2 OctoPrint upload client](https://github.com/OrcaSlicer/OrcaSlicer/blob/v2.4.2/src/slic3r/Utils/OctoPrint.cpp).
Native plugin status: [Orca printer connection plugin documentation](https://github.com/OrcaSlicer/OrcaSlicer/wiki/plugins_types).
