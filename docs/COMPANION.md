# Desktop companion and OctoPrint integration

Decision, 2026-09-15: move the archive client into a standalone desktop workflow.
Upstream Orca artifact hooks are an optional enhancement, **not a prerequisite
for shipping the companion**. The existing generic RFC remains a proposal;
its acceptance is unknown and the product no longer waits for it.

## Components and authority

| Component | Responsibility |
| --- | --- |
| Stock Orca | Prepare geometry/settings; save the editable project; generate the slice |
| Desktop companion | Preserve explicitly supplied inputs, receive exact uploads, review associations, queue archive transfers |
| Optional Orca plugin | Expose supported convenience actions and mesh snapshots as available; reuse the companion's archive protocol |
| Bridge library | Immutable artifacts, derived previews, downloads, history and per-attempt records |
| Existing native Bambu gateway | Printer state, inventory, command dispatch and interrupted-job recovery |

The companion reuses `plugins/bridge_library/library_plugin.py` as its archive
client. It does not duplicate storage/upload protocols or introduce a second
printer controller. Its executable can update independently of Orca.

## What OctoPrint contributes

Stock Orca already speaks the OctoPrint upload interface. Our existing server
adapter in `api/orca.py` implements that subset. The new desktop receiver uses
the same documented request shape, providing a supported handoff point outside
Orca's process. The desktop receives `file`, `path`, `print`, and, where supplied,
`plateindex`; those fields do not contain the original CAD or a full project.

Running a complete OctoPrint server is optional future interoperability, not
necessary for the Bambu path. A future OctoPrint plugin can archive received
files and observe that server's job lifecycle using the same library contracts.
For an OctoPrint-managed printer, OctoPrint should own physical dispatch; for
the existing Bambu gateway, it should remain the sole physical controller.
Do not run competing automatic queues against one printer.

The OctoPrint project's BambuConnector currently documents a dependency on
development changes for OctoPrint 2.0. Treat compatibility as version-specific;
do not introduce that dependency just to receive uploads we already support.

## Capture fidelity

An external application cannot read unsaved Orca project state through a file
upload protocol. The stock-compatible baseline is therefore: preserve selected
originals, save the prepared Orca project, receive the slice, and explicitly
associate those artifacts. The user does not need to create an archive folder,
but this baseline still has save/review actions.

The current preview copies inputs immediately on selection and freezes an
outbox manifest before transfer. The project may be older than Orca's live
state; show that it is a saved snapshot. Source files selected after import may
also have changed since import. Generated primitives have no original source.
Do not assert reslice qualification, complete provenance, or atomic normal-Print
capture merely because files and hashes exist.

Avoid private autosave parsing, process injection, global filesystem watching,
or filename/time matching. Two identical sends can be separate operations;
two different Orca windows can use identical filenames. Explicit receipt IDs,
selected-plate identity and content hashes must survive every handoff.

## Delivery sequence

1. **Standalone archive preview (implemented).** Desktop window, original/project
   selection, upload inbox, explicit association, persistent outbox, HTTPS
   Tailscale receiver and stock-OctoPrint wire tests. It accepts Upload and
   refuses Upload and print. File receipts are not print attempts.
2. **Qualify the managed Orca workflow.** Choose/import files through a companion
   session, launch an unchanged Orca installation, establish its project save
   destination through supported configuration, and make saved revisions easy
   to capture. Qualify the actual stock upload dialog, multiple windows/plates,
   modified inputs, modifiers, painted parts, mirrored instances and offline
   recovery. Implement persistent OS-protected pairing and local retention UI.
3. **Bridge review and dispatch.** Add a clearly reported pending-start handoff
   to the existing native replay review/queue. Keep fresh per-attempt AMS mapping,
   exact slice hashes and idempotent confirmation. Never report that Orca started
   printing when the companion merely archived or awaits mapping. Determine
   whether Upload plus bridge Start is the supported UI, or a qualified enhanced
   action is available; do not pretend those experiences are identical.
4. **Geometry and catalog.** Use verified project geometry for the existing
   fixed-zoom rotating corner render, with modifier filtering and cached assets;
   expose originals/projects/exact slices and attempts in dashboard and Android.
5. **Optional ecosystem adapters.** A thin supported Orca plugin improves capture
   fidelity and convenience when APIs exist. An OctoPrint archive/plugin adapter
   can serve other printers without changing the library contract. Neither
   requires the user to maintain an Orca fork.

The preview is a concrete implementation of step 1, not a claim that all five
steps are complete. Physical dispatch qualification remains separate.

## Primary references

- [Orca's OctoPrint upload implementation](https://github.com/OrcaSlicer/OrcaSlicer/blob/main/src/slic3r/Utils/OctoPrint.cpp)
- [OctoPrint file upload contract](https://docs.octoprint.org/en/main/api/files.html)
- [Orca plugin system](https://github.com/OrcaSlicer/OrcaSlicer/wiki/plugin_system)
- [Orca host model interface](https://github.com/OrcaSlicer/OrcaSlicer/wiki/host)
- [Official OctoPrint BambuConnector](https://github.com/OctoPrint/OctoPrint-BambuConnector)
- [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)
