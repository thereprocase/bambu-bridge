# Changelog

All notable changes to Bambu Bridge are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-09-10

- Add HTTPS on port 8443 with a persistent per-bridge identity and automatic
  certificate renewal, alongside the optional legacy HTTP listener.
- Add private, ten-minute, single-use pairing QR codes and separate revocable
  phone credentials. Paired credentials require HTTPS/WSS.
- Add owner/CLI device management and self-revocation; close revoked live sockets.
- Run both listeners with one application lifespan and one printer registry.
- Remove submitted values from validation diagnostics to protect pairing secrets.

## [0.1.3] — 2026-09-09

- Fix HTTP access logging with Uvicorn's native formatter. Credential redaction
  now preserves the five typed arguments that formatter requires; 0.1.2's
  interpolation caused access-log formatting errors even when requests succeeded.
- Exercise successful and unauthorized HTTP requests with the actual access
  formatter, and retain credential redaction in both structured and plain logs.


## [0.1.2] — 2026-09-09

- Restore the deployed MQTT reconnect overflow guard and 15-second retry cap.
- Restore structured HMS/stage decoding, job context, early-finish detection,
  and completed-layer false-positive suppression, while retaining the public
  snapshot contract that keeps the expected certificate fingerprint internal.
- Redact credentials before Uvicorn HTTP/WebSocket and structured log output.
- Release terminal job payloads and remove completed lifecycle tasks from memory.
- Bound buffered transfers to 64 MiB by default (BRIDGE_MAX_TRANSFER_BYTES),
  reject oversized uploads with 413, and serialize preview fills.
- Revalidate preview caches using remote size/modification metadata and directory;
  use content hashes in representation-specific ETags, invalidate on file changes
  and new prints, and re-fetch when firmware cannot report a revision.
- Parse preview geometry away from the API event loop.
- Validate updated printer connection fields and destinations before persistence.
- Encode Unicode download filenames safely; retain directory-aware file deletion.
- Keep the browser dashboard, viewer, and Home Assistant packages on matching source.

Operators upgrading an older deployment should rotate API keys that previously
appeared in request URLs after updating their clients. Existing journals are not
rewritten by this release.


## [0.1.1] — 2026-09-09

- Prepared a fresh public source history with operator data and private captures excluded.
- License the combined release under AGPL-3.0-only and retain third-party notices.
- Add corresponding-source links and a Gridline project site.
- Include complete notices in the Home Assistant build context.
- Document the limits of HMS provenance and hardware validation.

## [0.1.0] — 2026-06-13

Local development baseline, not a prior release from this repository. Covers the full P1S LAN bridge stack from wire protocol
to companion-app API and Home Assistant integration.

### Added

#### 3D Print-Progress Viewer
- 3MF mesh extraction and gcode toolpath parsing (`protocol/threemf.py`,
  `protocol/gcode_path.py`).
- Binary toolpath format with 4-byte-aligned header for efficient transfer.
- `VizCache` service pre-warms toolpath data in the background after a file
  is sliced and uploaded.
- ETag / 304 Not Modified + gzip compression on the viz endpoint; client-side
  timing instrumentation.
- React Native `postMessage` bridge for WebView-based viewer.

#### Layered Control Surface
- Four-tier safety model: GREEN (normal), YELLOW (caution), RED (stop-only),
  BLACK (emergency / e-stop); all guards are fail-closed.
- Advanced router (`api/advanced.py`) exposes tier-gated commands; filament
  management (`api/filament.py`) likewise tier-gated.

#### Sliced-Date Persistence and SD Listing
- Sliced dates persisted in SQLite and returned newest-first in the SD-card
  file listing (`api/files.py`, `slicedoc/`).

#### Home Assistant Integration and Add-on
- HA custom integration (`homeassistant/integration/`) with config flow, 15+
  entity platforms (sensor, binary sensor, camera, button, fan, light, number,
  select), diagnostics, and Lovelace example automations.
- HA Supervisor add-on (`homeassistant/addon/`): multi-arch Docker image
  (amd64 / aarch64 on Alpine 3.20 Python 3.12); vendor-sync script keeps
  add-on source in lockstep with the main tree.

#### Push Notifications
- ntfy dispatcher (`push/ntfy.py`) sends print-done / spaghetti-detected
  events to a self-hosted ntfy server.

#### Spaghetti Detection
- Optional computer-vision layer (`vision/`) wraps a local detector model;
  disabled by default (`spaghetti_detection: false`).

#### Release Infrastructure
- `Makefile` with `build`, `verify-dist`, `integration-zip`, `addon-vendor`,
  `addon-vendor-check`, `bump-version`, and `export-release` targets.
- `scripts/export-release.sh` produces a clean release tree via `git archive`
  with a safety guard that aborts on any sensitive-keyword hit.
- `deploy/install.sh` substitutes the invoking `$USER` into the systemd unit
  and installs it.
- `homeassistant/integration/release-zip.sh` packages the HA integration into
  a distributable zip.
- `RELEASING.md` documents the four version sites and the new-repo release
  flow (dev history never pushed).
- Full MIT `LICENSE` file; pyproject classifiers, keywords, and project URLs.
- `pytest-timeout` declared in dev dependencies; 30-second per-test timeout.

[Unreleased]: https://github.com/thereprocase/bambu-bridge/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/thereprocase/bambu-bridge/releases/tag/v0.1.1
