# Troubleshooting

Symptom on the left, fix on the right. Most problems are one of: the bridge URL
is wrong, the API key is wrong, the printer is unreachable, or Tailscale is off.

---

## "Unreachable" / red dot in Settings

The app can't reach your bridge at the URL you entered.

- **Check the URL.** It must include `http://` (or `https://`) and the `/api/v1`
  path — e.g. `http://your-bridge-host:8080/api/v1`. The default port is `8080`.
- **Is the bridge running?** Open the bridge's Base URL in a browser on the same
  network; you should get a response from the bridge, not a connection error.
- **Right network?** If you entered only a LAN URL, you have to be on that LAN.
  Add a Remote (Tailscale) URL so the app works when you're away.
- **Tailscale off?** If you're not on your home Wi-Fi, the app uses the Tailscale
  URL. Make sure Tailscale is connected on both the phone and the bridge host.

## "API key rejected" / yellow dot

The bridge was reachable but turned down your key.

- The key is your bridge's `BRIDGE_API_KEY`, found on the bridge host in
  `~/.config/bambu-bridge/bridge.env`. Copy that exact value into the **API key**
  field.
- Watch for stray spaces or a trailing newline when pasting. Re-paste and tap
  **Save & probe** again.

## The "Can't reach the bridge" banner keeps showing

The app can't reach the bridge on either the LAN or the Tailscale path and is
showing cached data.

- **"Phone has no network connection."** — Your phone is offline. Reconnect to
  Wi-Fi or mobile data.
- **"Join home Wi-Fi or turn on Tailscale."** — You have a network, but neither
  the saved LAN nor Tailscale is currently working. Get on your home Wi-Fi, or
  turn Tailscale on (phone *and* bridge host).
- The banner clears on its own once the bridge is reachable again.

## Adding a printer fails

The add-printer screen reports exactly which step failed:

- **"Can't reach the printer"** — The app reached the bridge, but the bridge
  couldn't reach the printer. Double-check the printer's **LAN IP**, and confirm
  the printer is powered on and on the same network as the bridge.
- **"Printer rejected the access code"** — The 8-digit access code is wrong.
  Read it again from the printer at **Settings ▸ WLAN** and retype it (no
  spaces).
- **"Printer connected but silent"** — The bridge connected but the printer sent
  no data. Make sure **LAN-Only Mode** is on (**Settings ▸ Network** on the
  printer), then try again.

## Printer shows offline / no live data

- Confirm the printer is on and on the same network as the bridge host.
- Confirm **LAN-Only Mode** is still on (a firmware update or factory reset can
  turn it off).
- Pull down to refresh on the Status tab. If the bridge itself is reachable
  (green dot in Settings) but the printer is offline, the problem is between the
  bridge and the printer, not your phone.

## The 3D view won't load

- The **View in 3D** button is only active while a print is loaded
  (printing/paused/preparing). If there's no job, it's intentionally disabled.
- If it opens but fails to render, the bridge couldn't serve the viewer right
  then — go back and tap **View in 3D** again in a moment.
- Make sure the bridge connection is healthy (green dot in Settings); the viewer
  uses the same connection as everything else.

## The camera image isn't showing

- The camera feed comes through the bridge from the printer. If the printer is
  offline or **LAN-Only Mode** is off, there's no feed to show.
- Confirm the bridge is reachable (green dot in Settings) and the printer is
  online on the Status tab.

## Jog / move buttons are disabled

- While a print is active, the jog buttons are locked on purpose. Pause or stop
  the print first.
- If a Z- (move bed toward nozzle) move asks you to confirm, that's a safety
  guard for when the app doesn't know the current position — it can't rule out a
  nozzle/bed crash, so it asks before moving.

## The app crashes immediately on launch

You're probably trying to run it inside **Expo Go**. This app needs its custom
build — install the `.apk` (see [INSTALL.md](INSTALL.md)) instead. Expo Go can't
run the on-device storage the app relies on.

---

If a problem isn't listed here, the message the app shows usually came straight
from your bridge — it's worth reading in full, and checking the bridge server's
own logs for the same event.
