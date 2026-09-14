# Bridge Library plugin development build

`library_plugin.py` is a standalone PEP 723 Orca plugin. The official Windows
nightly dated 2026-09-14 loaded its native page successfully. Stable Orca 2.4.2
cannot load Python plugins. Use a separate official development installation
and data directory for testing.

The page can browse this plugin client's captures and retry its local outbox.
**It does not yet capture imports, export full projects or intercept Print.**
The local `Outbox` class accepts explicit artifacts from a future capture
adapter; it never scans user folders or Orca's recovery backups. The original
files can disappear after successful enqueue without breaking retries.

On a supported development build, install the file through File > Plugins and
enable the Bridge Library page. In the capability's Config tab set:

```json
{
  "url": "https://your-bridge.your-tailnet.ts.net",
  "printer_id": "YOUR_PRINTER_ID",
  "upload_key": "YOUR_SCOPED_ORCA_UPLOAD_KEY"
}
```

Create an upload-only Orca credential in the bridge settings. Do not use the
bridge owner key. Permission prompts should be limited to the chosen HTTPS
bridge and artifact files explicitly offered by the capture adapter; the
plugin's own storage is host-managed. Credentials are never added to URLs or
returned to the page. The default config editor is a development interface;
polished pairing and credential storage are release gates.

The queue uses 4 MiB chunks, verifies local hashes before transfer, resumes from
server offsets and writes its delivery receipt atomically after finalization.
It never invokes a printer command. Delivered local copies remain inside its
4 GiB quota until an explicit future retention action; none are silently removed.

See [implementation status and API contract](../../docs/PRINT-LIBRARY.md) and
[the proposed generic Orca extension](../../docs/ORCA-ARTIFACT-HOOK-RFC.md).
