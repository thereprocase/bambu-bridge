# Secure local Android pairing

Bridge 0.2.1 and Android 0.19.0 add encrypted local connections without a domain,
VPN, or installing a certificate on the phone. The phone scans the bridge's
identity once; status, controls, camera, and the embedded 3D viewer all verify it.

## Pair from the dashboard

Open the bridge dashboard over HTTPS and sign in with its owner API key.
Choose **Settings → Phones & pairing → Pair a phone**. Select **Home Wi-Fi**
at home, or **Tailscale / away from home** when configured and connected to
Tailscale. In Android, open **Settings → Pair with QR code**, scan, name the
phone and tap **Pair securely**. On the same phone, expand **Using this page
on the same phone?** and copy/paste the pairing code instead.

Codes expire after ten minutes and work once. The dashboard shows a countdown,
hides inactive codes, and lists paired phones below. Use **Cancel this code**
to invalidate an unused code or **Revoke access** beside a phone to disconnect
it. Leaving Settings cancels the displayed code when the bridge is reachable.
Codes stay in page memory and are served with `Cache-Control: no-store`.

Dashboard setup uses the bridge's direct HTTPS listener. Administrators can set
`BRIDGE_PAIRING_URL` to its LAN HTTPS API address and optionally
`BRIDGE_PAIRING_REMOTE_URL` to its direct Tailscale HTTPS API address, both ending
in `/api/v1`. The LAN address otherwise defaults to the host's route address.
Do not use a TLS-terminating proxy URL for these settings: its certificate key
differs from the bridge identity scanned by the phone. The dashboard itself may
use that publicly trusted HTTPS proxy. The optional remote URL configured later
in Android may also use it, with ordinary public certificate validation.

Anonymous visitors, read-only viewer tokens and paired phone credentials cannot
create pairing invitations or manage devices. The owner API key and HTTPS are
required; HTTP dashboards explain how to switch to HTTPS for these actions.

## Installer and SSH pairing

1. Install/start the bridge with `bambu-bridge` (the bundled systemd installer
   and Home Assistant add-on use this entrypoint). It serves HTTPS on port 8443
   and retains the existing HTTP listener on 8080.
2. The interactive Linux installer displays a QR and saves a private HTML copy.
   Connect the phone to home Wi-Fi. In the Android app, open **Settings → Pair
   with QR code**, scan it, name the phone, and tap **Pair securely**.
3. Add/select your printer in the app. Remote access is optional.

For an existing install, generate a fresh code on the bridge host:

```sh
bambu-bridge --env-file ~/.config/bambu-bridge/bridge.env pair \
  --output ~/pair-phone.html --terminal
```

Use the same environment and data directory as the running service. Older
installations may need `BRIDGE_DB_PATH` set explicitly to their existing DB.
On multihomed hosts, supply `--url https://YOUR-LAN-HOST:8443/api/v1`.
Home Assistant containers keep pairing state in `/data/pairing`; run the command
inside the add-on with its runtime environment.

The QR lasts ten minutes and can enroll one phone. Open its HTML file locally
on the bridge host or transfer it through an already trusted connection such
as SSH. Keep the file private. Do not upload it, serve it from an unauthenticated
HTTP page, or put it in logs. The API never provides anonymous pairing codes.
The terminal option requires a real interactive terminal to avoid log capture.

The app also accepts the code as text, accessible under the QR's **Use a pairing
code instead** disclosure. QR scanning is entirely offline using ZXing.

## Optional remote HTTPS

After pairing, Settings accepts an optional Tailscale HTTPS base URL. Use a
hostname with a valid public certificate. At home the app tries paired HTTPS on
Wi-Fi; outside it uses the remote address. Read requests may fail over once.
Printer commands are never automatically replayed after an uncertain response.

A reverse proxy terminating HTTPS must forward the original protocol. Set
`BRIDGE_TRUSTED_PROXIES` to that proxy's exact source IP(s), separated by commas;
the default trusts none. Its upstream hop must be loopback, encrypted Tailscale,
or another protected connection. Never use a wildcard or an entire home subnet.
This setting is unnecessary for direct local HTTPS.

## Compatibility and browsers

Paired credentials work only over HTTPS/WSS. The original owner API key and
read-only viewer key retain their existing behavior for older clients. Manual
HTTP setup remains an explicit compatibility choice in Android; it does not
receive or bypass the paired identity. Set `BRIDGE_HTTP_ENABLED=false` once
your other integrations no longer need HTTP.

The automatic local certificate is trusted by the paired Android app. Ordinary
browsers do not acquire that trust from pairing: use Tailscale's trusted HTTPS
address for the browser dashboard, or retain your existing LAN compatibility URL.
An external `uvicorn bambu_bridge.main:app` invocation does not start the new
dual listener; use the `bambu-bridge` entrypoint.

## Remove a phone and recover

Use **Revoke access** in the dashboard, **Disconnect and revoke this phone**
in its app, or run on the bridge host:

```sh
bambu-bridge --env-file ~/.config/bambu-bridge/bridge.env devices
bambu-bridge --env-file ~/.config/bambu-bridge/bridge.env revoke DEVICE_ID
```

The CLI never lists credentials. The owner API key can also manage
`GET /api/v1/pairing/devices` and `DELETE /api/v1/pairing/devices/{id}`. Paired
phones cannot enumerate or revoke other phones. Revocation rejects subsequent
HTTP requests and closes existing status WebSockets within approximately one second.

Back up the private `pairing/` directory alongside the printer DB. It contains
the stable TLS private key, its certificate, and a separate device database.
Use a consistent filesystem snapshot or stop the bridge while copying it.
The legacy `deploy/backup.sh` backs up the jobs DB only; it does **not** replace
this identity backup. Protect backups like credentials. Restore the directory
with its original key, owner and permissions before starting the bridge.

The leaf certificate renews automatically with the same public key; paired
phones keep working. A missing key beside an existing certificate stops startup
rather than silently replacing the identity. If the key is lost or deliberately
rotated, generate new pairing codes. If the host's LAN address changes, reserve
its address in the router or pair again with a fresh code; automatic discovery
of a moved bridge is not part of this release.

## Security contract

- P-256 TLS key per bridge; the QR carries its SHA-256 SPKI fingerprint.
- A random 256-bit invitation, hashed at rest, expires and is consumed atomically.
- Each phone receives its own random 256-bit credential. Only its hash is kept
  on the server; Android stores its complete paired profile in SecureStore.
- Local trust is limited to the paired HTTPS origin and API paths. No global
  trust-store changes, certificate-error bypass, redirects, or HTTP downgrade.
- Camera/API/WebSocket traffic uses the same native transport. The paired
  WebView intercepts every resource through that transport, permits only the
  selected printer's read routes, and blocks other navigation and network access.
- Pairing proves the bridge identity to the app. It does not change the separate
  printer LAN protocol, protect a compromised host/phone, or replace physical
  printer checks.
