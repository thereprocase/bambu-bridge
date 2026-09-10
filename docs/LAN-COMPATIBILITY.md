# LAN compatibility and access

Reviewed 9 September 2026. Bambu Bridge 0.1.1 targets the P1S local interfaces.
It is independent community software and is not endorsed or supported by Bambu Lab.

## Configure access on your printer

Enable LAN-Only Mode and, when your firmware offers it, Developer Mode on the
printer itself. Enter the printer's own LAN IP and access code in the bridge.
This release accepts eight numeric digits; other code formats and current
firmware compatibility remain unvalidated. The bridge's API key is a separate
secret that protects access to the bridge; it is not a Bambu account credential.

Bambu describes Developer Mode as an option for direct third-party MQTT, FTP
and camera access, with local-network security left to the operator and no
official protocol support. See its [January 2025 announcement](https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/)
and [mode setup guide](https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode).
The wiki page could not be retrieved during this review; consult it for your
current firmware's menus. LAN-only operation disconnects the printer from Bambu
Cloud, so this project does not promise simultaneous cloud printing or Handy
remote monitoring. Local slicing remains a separate workflow.

## What this release does

The reviewed code connects to the operator-selected printer using MQTT/TLS on
8883, implicit FTPS on 990, and the P1S camera on 6000. MQTT identifies itself as
`bambu-bridge-<random UUID>`. These connections authenticate with the access code
entered by the owner. Registration restricts the destination to a LAN address
by default; the loopback exception exists for local test services.

No Bambu Cloud login, official-client identity spoofing, authorization-control
bypass, automatic Developer Mode activation, access-code guessing, proprietary
networking plugin, or firmware binary is included. Optional operator-configured
notifications are separate from printer access. The test suite uses mock
services; it does not establish current firmware compatibility.

## Licensing and the wider dispute

The complete bridge source is distributed under AGPL-3.0-only, with retained
upstream notices. [THIRD_PARTY.md](../THIRD_PARTY.md) documents the small HMS
lookup's uncertain provenance and firmware-validation limits.

In [May 2026](https://blog.bambulab.com/setting-the-record-straight-on-cloud-access-and-community/),
Bambu affirmed AGPL source redistribution while objecting to a fork it alleged
impersonated an official client when accessing its cloud. That is Bambu's stated
position, not an adjudicated legal conclusion.

The [Software Freedom Conservancy disputes Bambu's restrictions and licensing
conduct](https://sfconservancy.org/news/2026/may/18/bambu-studio-3d-printer-agpl-violation-response/)
and is supporting alternative implementations. AGPL therefore must not be
presented as a guarantee against demands or disputes. Our assessment is limited
to this reviewed LAN implementation and its included materials; it is not
patent clearance or a guarantee of future vendor behavior.
