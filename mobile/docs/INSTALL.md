# Installing Bambu Bridge on your phone

Bambu Bridge for Android is distributed as a sideloaded `.apk` — you download
the file and install it yourself rather than getting it from the Play Store.
This takes about two minutes.

You'll need:

- An Android phone.
- The latest `bambu-bridge-vX.Y.apk` file.
- Your bridge's **Base URL** and **API key** (see [Connecting](#first-launch-connecting)).

---

## 1. Download the APK

Get the [latest ARM64 APK](https://github.com/thereprocase/bambu-bridge/releases/latest) onto your phone — download it directly in
the phone's browser, or transfer it from a computer (USB, or a shared folder).
Note where it lands; it's usually in your **Downloads** folder.

## 2. Open it and allow the install

1. Tap the `.apk` file (in your browser's downloads, or in the **Files** app
   under Downloads).
2. The first time, Android blocks it and shows something like *"For your
   security, your phone isn't allowed to install unknown apps from this
   source."* Tap **Settings**.
3. Turn on **Allow from this source** (this permission is tied to whichever app
   you opened the APK from — your browser or Files app).
4. Press back. Now tap **Install**.
5. When it finishes, tap **Open**.

> Verify that the download came from the project's GitHub release. The release
> includes a SHA-256 checksum and the signing-certificate fingerprint.

## 3. (Recommended) Turn the permission back off

Once installed, you can revoke the "install unknown apps" permission for your
browser/Files app — the Bambu Bridge app keeps working. On most phones:
**Settings ▸ Apps ▸ Special access ▸ Install unknown apps**.

---

## First launch: connecting

The app opens on the **Settings** screen, because it needs to know where your
bridge is before it can do anything.

1. **Remote base URL (Tailscale)** — enter how your bridge is reachable from
   anywhere, e.g. `http://your-bridge-host:8080/api/v1`. Include the `/api/v1`
   path. The default port is `8080`.
2. **LAN base URL (optional)** — enter your bridge's address on your home
   network, e.g. `http://192.168.1.50:8080/api/v1`. The app uses this faster
   path automatically when you're home and falls back to the Tailscale URL when
   you're out. You can skip this and add it later.
3. **API key (bearer)** — paste your bridge's key.

   The key is the `BRIDGE_API_KEY` from your bridge host. On the machine running
   the bridge, it's in:

   ```
   ~/.config/bambu-bridge/bridge.env
   ```

   Copy the value of `BRIDGE_API_KEY` from that file.

4. Tap **Save & probe**. A green dot and **Reachable** mean you're connected. A
   yellow dot (**API key rejected**) means the key is wrong; a red dot
   (**Unreachable**) means the URL or network is the problem — see
   [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Add your printer

Still in Settings, tap **Add a printer**:

- **Host (IP)** — your printer's LAN IP address.
- **Access code** — the 8-digit code on the printer at **Settings ▸ WLAN**.
- **Friendly name** (optional) — e.g. "Workshop P1S".

Your P1S must have **LAN-Only Mode** on (**Settings ▸ Network** on the printer).
Tap **Probe & connect**. Once it succeeds you'll land on the dashboard.

## Save your home network (optional but recommended)

Back in Settings, while you're on your home Wi-Fi, probe the **LAN URL**, then
tap **Save this network**. After that the app uses the fast LAN path at home and
the Tailscale path everywhere else, switching automatically.

## Updating

To install a newer version, just download the new `.apk` and open it — it
upgrades in place and keeps your settings, saved networks, and printers. You do
not need to uninstall first.
