# Proposal: optional artifact capture for stock Orca plugins

Status: upstream discussion draft, not an implemented API. Names below are
proposals, not callable bindings. No custom Orca binary is required by the bridge.

## User outcome

A user presses Print normally. An optional plugin can preserve the prepared
project and imported originals alongside the exact outgoing sliced job, for
reslicing, print history and visualization. This is useful for local archives,
print farms, asset managers and revision tracking, independently of any printer
brand or bridge. Plugin failure must not accidentally send a job twice.

## Existing extension points and the small gap

The plugin architecture already supplies pages, printer providers, model/mesh
inspection and slicing callbacks. The default print packaging intentionally
omits model data. The post-processing callback supplies a working G-code path,
but not a consistent full project snapshot or the final packaged upload.

At inspected development commit `292cf0095e698a6e0f96041bd142fd41afd6ccfb`, the
public `Plater` binding provides model and dirty-state inspection; it does not
bind full project export. The source does contain native project exporters.
The official Windows nightly dated 2026-09-14 also loaded our diagnostic plugin
and returned those same four public Plater members at runtime. That check used
an empty bed; it does not establish full geometry or project-capture support.
We should reuse those serializers rather than replicate Orca's modifiers,
painting, custom layers, embedded presets and project metadata in Python.

## Suggested minimum API

1. A **read-only project artifact export** supported by the host. Return an
   opaque snapshot identifier, selected plate identity, and a scoped, temporary
   self-contained 3MF artifact. Preserve project filename, dirty state, undo
   history and cached slice state. Use the existing secure settings export;
   do not disclose account credentials or full local paths by default.
2. An **optional pre-upload artifact callback**, after the final print payload
   is packaged, associated with the same captured project revision and plate.
   Supply a unique send-attempt identifier and read-only artifact handles, not
   mutable GUI pointers. Invoke for the normal Print flow regardless of the
   selected printer provider. Exports, canceled sends and repeated sends remain
   distinguishable. A native UI extension could show the callback's short
   archival status without exposing arbitrary controls inside the Print dialog.
3. An **optional import provenance callback**, only while an enabled plugin has
   requested original preservation. Describe input artifact(s) and their object
   associations, including archive containers. Allow copying the bytes while
   still available. Generated shapes should explicitly have no original file.
   A print-time source filename alone cannot preserve a file deleted since import.

We would start with (1), agree the callback lifecycle for (2), and add (3) as
the optional originals feature. No bridge-specific URL, authentication protocol,
storage policy, or renderer belongs in Orca core. No change to Bambu's payload
format or printer network module is necessary.

## Lifecycle and failure behavior

- Capture a consistent revision on the owning thread; serialize/copy immutable
  data without holding the UI across network operations. Do not call GUI APIs
  from a slicing callback whose caller may be waiting on the UI.
- The plugin obtains its own durable copies before temporary handles expire.
  The host retains no live model references on the plugin's behalf indefinitely.
- Report `saved`, `queued`, `skipped` or `failed` separately from upload/start
  status. Default to normal printing plus visible queued archival retry.
  An explicitly selected archive-required policy can prevent dispatch before it
  happens. A retry of archival work never repeats printer dispatch.
- A plugin receives only explicitly requested artifact categories, under the
  existing permission system. Keep source paths and credentials out of public
  provenance. Revoking the plugin prevents new capture.
- Capture IDs are opaque correlation tokens. Content hashes identify actual
  bytes. Names/timestamps alone cannot associate a project to a print.

## Acceptance cases for an upstream contribution

Unsaved project; two Orca windows; multiple plates; repeated Print without
reslicing; export without printing; cancel during preparation; modified project
during upload; source moved or deleted; painted multi-material model; negative
volume and support modifier; custom layer heights; mirrored instances; canceled
or failed serializer; plugin unload; no plugin installed (unchanged behavior).
Reopen the produced project and compare geometry, transforms, painting and
resolved settings. Verify unchanged dirty state, filenames and slice output.

## Proposed integration on the plugin side

The plugin turns these artifacts into a versioned manifest plus content hashes
and copies them into a bounded local outbox. HTTPS upload and retry run outside
Orca's UI. A supported plugin page browses the archive. The bridge does not
depend on undocumented Orca backup folders, memory layouts or UI automation.
It also preserves logical filament indices separately from a later replay's
physical spool mapping; the archival hook does not need to alter toolpaths.

## Source references

- [Host bindings](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/plugin/host/PluginHostApp.cpp)
- [Model bindings](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/plugin/host/PluginHostModel.cpp)
- [Print packaging and project export](https://github.com/OrcaSlicer/OrcaSlicer/blob/292cf0095e698a6e0f96041bd142fd41afd6ccfb/src/slic3r/GUI/Plater.cpp)
- [Plugin system](https://github.com/OrcaSlicer/OrcaSlicer/wiki/plugin_system)
- [Slicing callback constraints](https://github.com/OrcaSlicer/OrcaSlicer/wiki/slicing)
