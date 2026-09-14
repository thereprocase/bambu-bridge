# AMS statistics and Android distribution

Snapshots now include `ams.units[]`: `id` is the firmware unit ID, `humidity_pct` is numeric percent RH from `humidity_raw`, and `temperature_c` is Celsius from `temp`. Missing, nonfinite, boolean and out-of-range values are null. The firmware humidity category is never treated as a percentage. Existing slot and external-spool fields are unchanged.

The dashboard shows environmental readings per AMS, including when no slot is engaged. Disconnected readings are marked last known. Android 0.20.2 shows the same readings on Status and Filament, and tolerates older servers without this optional field.

To offer a signed Android update, install the verified APK as `/var/lib/bambu-bridge/downloads/bambu-bridge.apk`, readable by the service. Replace it atomically after checking its package, increasing version code, signer and SHA-256. The dashboard and onboarding screen link to `/downloads/android`. This fixed download route exposes only the APK, requires no pairing, and returns 404 when no artifact is installed. Do not put signing keys or paired credentials in the APK or source tree.

Host the dashboard behind private HTTPS with WebSocket support. Keep existing bridge authentication and native HTTPS pairing endpoints. A separate private HTTP shortcut may redirect to this HTTPS origin. Do not attach it to a public Funnel route.
