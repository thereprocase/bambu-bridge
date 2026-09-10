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

## Connect Orca

Open the bridge dashboard over HTTPS, then **Settings → Orca · native P1S**.
Select your P1S and check **Expose to Orca as a P1S**. Copy the server address,
printer serial and separate native access code. The code is shown once;
**Replace native access code** disconnects existing clients and creates another.

In OrcaSlicer, use the Bambu Lab P1S preset and turn **Use 3rd-party print host**
off. Use Orca's normal LAN printer connection with the bridge's address and
native code. Open Orca's printer list and click **Find in Orca on this computer**
in the dashboard if the printer is not discovered. Open the dashboard on the
same computer as Orca; this sends a private unicast discovery announcement,
which can cross Tailscale without multicast routing.

Slice and open **Print plate**. The gateway forwards live printer reports and
the native print command, including plate selection, AMS slot mapping and the
external-spool choice. There is no fixed mapping attached to this connection.
The Device view receives the printer's status and shared camera stream.
For a four-color AMS job, map the four slicer filaments to the four AMS slots.
For an external-spool job, choose the external spool instead. Both use the
same endpoint and code; a job uses one of these source modes.

This is a preview. Live checks on a P1S verified AMS/external status, a read-only
command response, camera JPEG frames and an FTPS file listing. Wire-level tests
cover MQTT command forwarding and upload/download with real TLS fixtures.
A successful physical print through the installed Orca UI remains a separate
acceptance step. See [VALIDATION.md](../VALIDATION.md) for evidence.

## Server setup

Set `BRIDGE_NATIVE_HOST` to one private IPv4 address owned by the bridge server,
preferably its Tailscale address. Pairing storage must be enabled. The switch
starts TLS listeners on TCP 8883 (MQTT), 990 (implicit FTPS) and 6000 (camera).
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
on the server. The separate native code is hashed at rest and is not an owner
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


## When Orca cannot connect

Use v0.3.1 or newer for RSA-compatible native TLS. The gateway keeps this
certificate separate from phone pairing and preserves the existing native
access code when updating from v0.3.0.

On the computer running Orca, verify that the bridge's private address is
reachable over Tailscale and that TCP 8883, 990 and 6000 are allowed. Opening
the dashboard through an HTTPS proxy does not by itself prove these native
ports are reachable. Use the generated native code, not the printer's own code.

In dashboard Settings, click **Check Orca connection** after trying Connect.
It reports completed secure connections, rejected codes and protocol errors
without recording credentials or peer addresses. If no secure connection has
completed, check the address, network access and TLS compatibility first.
