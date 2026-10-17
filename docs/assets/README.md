# Interface preview provenance

These are captures of Bambu Bridge v0.1.1 application components, with synthetic
data supplied in an isolated local browser. No printer or running bridge was
contacted. No camera image, serial number, access code or API key is included.

- `dashboard.webp`: real dashboard and store renderer. Camera and recent-job
  panels are omitted to focus the preview on status and controls. Sample values
  show 62 percent, layer 186 of 300, and 38 minutes remaining.
- `viewer.webp`: real WebGL viewer, showing an original procedural fluted vessel
  at layer 186 of 300. The mesh has 96 points per ring and 61 rings, height 60 mm.
  Its radius is `22 + 3*cos(12*angle + 1.4*h) + 5*sin(pi*h)` mm, where `h` runs
  from zero to one. It is a visualization fixture, not a supplied print job.

The gold portion follows reported build height; the translucent portion is the
remaining model. The mesh-mode red marker is symbolic, not measured nozzle XY.
The interface's “live” indicators are driven by the synthetic fixture.

Images use lossless WebP, with dimensions and SHA256 recorded in
`preview-provenance.json`. They are supplied under this repository's AGPL license.
