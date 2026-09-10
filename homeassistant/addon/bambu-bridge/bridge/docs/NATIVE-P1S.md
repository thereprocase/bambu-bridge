# Native P1S access in OrcaSlicer

**One endpoint, one native access code, a choice for every print:** a four-color
AMS job or an external-spool job. Keep the same printer entry in Orca for both;
choose the source in **Print plate**, without changing the server connection.

With Tailscale, the IP belongs to **the bridge server**. Orca connects to that
server over the tailnet; the bridge communicates with the physical P1S over its
LAN address. The printer itself does not run Tailscale. Uploads, commands,
filament status and camera frames pass through the gateway.

```mermaid
flowchart LR
    O[OrcaSlicer] <-->|Tailscale| B[Bridge server]
    B <-->|Local network| P[P1S printer]
```

## A second printer, with a separate identity

Orca shows **P1S** for the physical printer and **Bridge P1S** for the server.
The bridge has its own persistent 15-character virtual serial. Local discovery
of the physical printer cannot overwrite the bridge entry because their IDs
are different. Discovery, identification, the native certificate, MQTT topics
and the setup command all use this virtual identity. Use the dashboard's
**Printer serial** for manual setup, never the physical printer's serial.

**Upgrading from 0.4.0 or earlier:** run a fresh Windows setup command on each
computer, then choose Bridge P1S. The native access code stays the same, but
the new identity has a new certificate. Older entries using the physical serial
are not the new bridge printer. Back up the protected pairing directory to
preserve the virtual identity across server moves and restores.

The gateway answers access-code queries with the native bridge code. It does
not expose the physical printer's password through native status replies or
let that password replace Orca's bridge credential.

## Connect Orca on Windows

1. On the computer where Orca will run, open your bridge dashboard over HTTPS.
   Away from home, connect that computer to your Tailscale network first.
2. Open **Settings > Connect Orca to your printer**. Select your P1S and turn on
   **1. Enable Orca access**.
3. Save your work and close Orca. Click **Copy Windows setup command**. Open
   Start, type **Windows PowerShell**, and open it normally. Paste and press
   Enter. If Windows requests permission to update Orca's certificate file,
   choose **Yes**. Wait for the green **Ready!** message.
4. Open Orca > **Device** > select **Bridge P1S**. Use a Bambu Lab P1S preset
   with **Use 3rd-party print host** off. Press **Play** in the camera panel.
5. Return to the dashboard and click **Check this computer**. The printer and
   camera checks are separate; another computer's connection does not count.

The helper registers the printer, corrects a saved LAN address, saves the same
native code in Orca, and trusts this bridge's exact certificate in Orca's
printer bundle. It verifies the certificate against the fingerprint supplied
by your authenticated dashboard and checks the printer and camera ports.
Other printers and existing certificates are preserved, with backups next to
changed files. It never force-closes Orca, rotates the code, starts a print,
or adds a Windows-wide trust anchor. Only the certificate update is elevated;
your profile is updated as the user running the original PowerShell window.

Open Orca once before using the helper so its profile exists. If Orca is in a
custom installation folder, the helper asks you to select `orca-slicer.exe`.
A portable/custom data-directory profile needs manual setup. Windows is the
currently automated path; macOS/Linux instructions are below.

Run setup on each computer. After an Orca update replaces its bundled
certificates, run a fresh setup command from the dashboard. The command
contains the native printer access code: keep it private, just as you would
that code. It does not contain the bridge owner API key or physical printer's
access code, and it is never stored in browser local storage.

## Manual setup and other operating systems

Expand **Manual setup, access code and troubleshooting** for the server
address, serial, **Show/Copy native access code**, and **Download bridge
certificate**. Close Orca and back up its `resources/cert/printer.cer`, then
append the downloaded certificate while keeping all existing certificates.
On macOS the resources are inside `OrcaSlicer.app/Contents/Resources`; on Linux
use the resources of the extracted application. A package manager or app update
may replace that file. Do not replace the entire bundle with the bridge leaf.

In Orca's **Connect the printer using IP and access code** dialog, enter the
bridge's address and native code. If it offers **Manual Setup**, use name
**Bridge P1S**, the dashboard's virtual serial, and model **Bambu Lab P1S**. The code is
eight alphanumeric characters; the serial has fifteen characters. The bridge
answers native identification on TCP 3000 where the installed plugin supports
that lookup. The Windows helper avoids this two-screen flow by registering
the entry directly.

**Find in Orca on this computer** sends private unicast discovery to the
computer displaying the dashboard. Discovery depends on that computer's
firewall; it is optional with the Windows helper. A send confirmation is not
proof that Orca received the announcement.

One native code supports multiple computers and survives bridge restarts.
Replacing it disconnects clients and requires updating each computer. Older
hash-only installations can use **Save existing code** without rotation;
a successful native reconnect also saves the verified code for later copying.

For a four-color AMS print, map the slicer's filaments to the live AMS slots in
**Print plate**. For an external-spool print, choose the external spool. Both
use the same endpoint. No fixed mapping is attached to the server connection.

## Address isolation

Native clients must keep using the configured bridge address. The gateway
transposes physical addresses in report strings and URLs, including alternate
interfaces disclosed by the printer. It also rewrites the little-endian IPv4
integers in `print.net.info[].ip` on both initial snapshots and later reports.
Orca otherwise consumes that field and silently restores the physical LAN
address after MQTT connects, breaking camera access and future reconnects.
The virtual interface advertises a host mask, without the physical router.
Unconfigured zero addresses and unrelated numbers are preserved.

Native identification, discovery, camera listeners and passive FTPS endpoints
use the same configured bridge address. Commands retain their plate, file and
AMS semantics, and successful uploads are acknowledged only after the printer
receives them. This is the supported P1S gateway protocol boundary, not a claim
of compatibility with every Bambu model or future firmware feature.

Live acceptance has confirmed an offsite Orca printer connection, plus direct
native TLS camera frames and FTPS file listing. The numeric-address correction
has wire regression coverage. Actual Orca camera playback after this update
and a completed physical print remain separate user acceptance steps. See
[VALIDATION.md](../VALIDATION.md).

## Server setup

Set `BRIDGE_NATIVE_HOST` to one private IPv4 address owned by the bridge server,
preferably its Tailscale address. Pairing storage must be enabled. The switch
starts TLS listeners on TCP 8883 (MQTT), 990 (implicit FTPS) and 6000 (camera),
plus a read-only identity listener on TCP 3000. This last protocol uses native
plaintext framing on the private address; it returns model, name, serial and
firmware only, accepts no control or cloud-binding commands, and never returns
access codes or keys. Printing, camera and files still require the native code.
Passive FTPS data ports are allocated on the same address. Clients must be
able to reach these ports directly; an HTTPS reverse proxy alone is insufficient.
Discovery is UDP to ports 1990 and 2021 on the requesting computer. Its firewall
must allow Orca to receive discovery from the bridge.

For systemd services running as a regular user, port 990 requires a service
override with `CapabilityBoundingSet=CAP_NET_BIND_SERVICE` and
`AmbientCapabilities=CAP_NET_BIND_SERVICE`. Keep `NoNewPrivileges=yes`.
Do not run the service as root to solve the port permission. For containers,
use appropriate private host networking and that one capability. The Home
Assistant add-on does not expose this optional configuration in its settings yet.

Native mode gives full printer control to anyone holding its eight-character
code. Keep these listeners on a trusted private network or Tailscale; do not
forward them to the public internet. The printer's original access code remains
on the server. Native authentication uses a hash. A recoverable copy is stored
encrypted for the owner-only HTTPS Show/Copy endpoint, with caching disabled.
Back up the protected pairing directory including `native-code.key`.
The separate native code is not an owner
API key or a phone pairing token. Setup requires owner authentication over HTTPS.
The mode and its code survive restarts. Disabling the checkbox closes native
connections; an already running print continues on the printer.

The gateway reuses the bridge's MQTT and camera sessions and its P1S FTPS client.
Uploads are acknowledged only after reaching the printer. Native commands use
the printer's own validation and acknowledgement path; the separate REST job
submission validator is not applied to the native protocol. No proprietary
networking library, printer credential or vendor signing key is distributed.

The older [HTTPS upload adapter](ORCA.md) remains available for clients that
only need upload or a fixed mapping. Choose one connection style in Orca.


## Troubleshooting

Use v0.4.1 or newer for guided setup and packed-address isolation. Native TLS uses RSA compatibility. The gateway keeps this
certificate separate from phone pairing and preserves the existing native
access code when updating from v0.3.0.

On the computer running Orca, verify that the bridge's private address is
reachable over Tailscale and that TCP 3000, 8883, 990 and 6000 are allowed. Opening
the dashboard through an HTTPS proxy does not by itself prove these native
ports are reachable. Use the generated native code, not the printer's own code.

Start with **Check this computer**. For additional protocol details, expand
Manual setup and click **Check Orca connection** after trying Connect.
It reports identity lookups separately from completed secure connections,
rejected codes and protocol errors, without recording credentials or peer
addresses. If identification fails before a secure connection opens, check
TCP 3000 and use v0.3.2 or newer. If identification succeeds but MQTT does not,
check TCP 8883, the native code and TLS compatibility.

The two-screen flow is documented by
[OrcaSlicer v2.4.2's dialog implementation](https://github.com/OrcaSlicer/OrcaSlicer/blob/v2.4.2/src/slic3r/GUI/ReleaseNote.cpp#L1911).
The independent [Open Bamboo networking protocol research](https://github.com/ClusterM/open-bamboo-networking/blob/master/research/08.06-bind.md)
documents the native identity request and response framing. No vendor plugin
code is bundled with this bridge.
