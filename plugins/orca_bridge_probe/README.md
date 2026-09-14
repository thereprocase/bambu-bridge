# Bridge Library Compatibility Probe

This is a read-only Sprint 0 diagnostic, **not the archive plugin**.
OrcaSlicer 2.4.2 cannot load it: that release predates the Python plugin system.
The source is matched to Orca development revision
`292cf0095e698a6e0f96041bd142fd41afd6ccfb`; execution in a compatible Orca build
loaded successfully in the official Windows nightly dated 2026-09-14. The
Bridge Compatibility page executed the read-only probe against an empty bed.
That proves host loading and API inspection, not mesh capture on a populated
plate, project round-trip recovery, or automatic print capture.

On a plugin-enabled official build, install `probe.py` using File > Plugins,
then run the **Bridge Library Compatibility Probe** script capability with a
model loaded, or open its **Bridge Compatibility** page. It writes
`compatibility-report.json` into its own plugin storage.
It reports mesh counts, sample mesh coordinates, transforms and API members.
It does not save a full mesh/project, read original files, enumerate profiles,
use the network, alter geometry, slice, or send printer commands. Object names
and source paths are omitted from the report.

The report always marks full-project recovery, selected-plate association and
automatic print capture as unverified. A non-empty source path is not evidence
that the original bytes are preserved. Do not enable this as a slicing hook.

To inspect the installed binary without starting Orca:

```powershell
python probe.py --inspect-binary "C:/Program Files/OrcaSlicer/OrcaSlicer.dll" --output binary-report.json
```

This hashes the binary and checks supporting markers. Release-source evidence
is still required to determine compatibility. It makes no installation changes.
