# Native P1S gateway preview — 0.3.0

Native wire tests use real TLS MQTT, FTPS and binary camera clients with
isolated printer fixtures. They verify live AMS/external reports, unchanged
multi-plate print commands and AMS mappings, repeated acknowledgements,
camera framing, upload completion before success, download/delete, incorrect
code rejection, topic restrictions, code hashing, disable/restart state,
disconnecting active clients, private discovery and owner-only HTTPS setup.
No test starts a physical print. Ruff and mypy pass.

Chromium tests against the real HTTPS backend cover enabling the checkbox,
copying the one-time code, rotation, discovery and disabling. Layouts pass at
320, 1440 and 3840 pixels with no horizontal overflow or JavaScript errors.
This is protocol/fixture acceptance. Installed Orca UI compatibility and a
completed physical print require live acceptance; they are not inferred from
passing mocks. Native commands use printer validation, while the older HTTPS
adapter retains the REST job validator. See [native setup](docs/NATIVE-P1S.md).

## Previous release evidence

# OrcaSlicer print-host preview — 0.2.2

The OctoPrint upload contract used by stock OrcaSlicer 2.4.2 is exercised by
API tests: connection test, HTTPS and per-printer key enforcement, owner-only
key management, hash-at-rest and revocation, unchanged upload bytes, real FTPS
transfer to a disposable TLS server, and mocked submission into the existing
job lifecycle with an explicit AMS mapping. Unsupported selections, malformed
containers, thermal violations, busy/offline printers and changed certificate
identity are rejected without starting a print. Ruff and mypy pass.

Isolated Chromium exercises the real HTTPS backend's setup view: create/copy,
connection test, revoke, fixed mapping and one-time key display. Layouts pass
at 320, 1440 and 3840 pixels with no horizontal overflow or JavaScript errors.
No production credentials enter test fixtures. A physical print initiated by
the installed Orca UI has not been performed; no production print is started
by release checks. First preview: one sliced plate at position 1; no native
Orca AMS/device integration. See [setup and limits](docs/ORCA.md).

## Previous release evidence

# Dashboard pairing preview — 0.2.1

Focused pairing, dashboard shell, owner authentication and viewer-token tests
pass, as do Ruff and mypy. Isolated Chromium exercised the actual HTTPS bridge
and dashboard at widths 320, 1440 and 3840: QR rendering/copy, real invitation
claim, automatic device refresh, revoke, cancel and leaving Settings all passed
without JavaScript errors. HTTP dashboards made no pairing API requests.

The full Python 3.12/3.13 CI matrix gates publication. Tests additionally reject
anonymous/viewer/paired-device invitation creation, HTTP management, unconfigured
remote targets and Host/Forwarded-Host substitution. Codes and device lists are
served without caching; no production credentials enter browser test fixtures.

The user confirmed Android 0.19.0 could scan and claim a terminal-generated QR.
Physical-phone acceptance of the embedded paired 3D viewer remains separate.

## Previous pairing release evidence

# Local pairing preview — 0.2.0

The local Python 3.12 regression run passed **867 tests**, with **9 skipped**.
Additional pairing checks run against the frozen dependency environment cover
single-use claims under concurrency, expiration, identity persistence, HTTP
rejection, forwarded-header spoofing, owner/device authorization, revocation of
an open WebSocket, and secret-free validation errors. Ruff and mypy pass.

A separate real-listener fixture verifies HTTP compatibility, TLS, enrollment,
restart persistence, revocation, secret-free logs, and exactly one application
lifespan shared by the two listeners. It contains no real printer configuration.
The interactive installer shell passes `bash -n`.

Android 0.19.0 has native TLS tests and a signed ARM64 build. Physical-phone QR
scanning and paired WebView acceptance remain pending for this preview. Earlier
0.18.3 device evidence does not establish those new paths.

## Prior release evidence

# Validation — 0.1.3, 9 September 2026

The targeted release/HMS suite passes all 25 tests, including two regressions
using Uvicorn's actual HTTP access formatter for successful and unauthorized
responses. Both the native and plain formatters retain useful request/status
fields while excluding known and generic query credentials.

The release is gated by the complete Python 3.12/3.13 matrix, lint, typing,
distribution/vendor checks and credential scanning. [CI runs and test reports](https://github.com/thereprocase/bambu-bridge/actions/workflows/test.yml)
identify the tested commit for each release. Live read-only acceptance additionally
checks telemetry, preview delivery, authentication, WebSocket redaction and
absence of access-log formatter errors after deployment.

## Previous release — 0.1.2, 9 September 2026

The complete Linux suite passed on **Python 3.12 and Python 3.13**:
**852 passing tests and nine skips on each version**. Ruff and strict mypy also
passed; mypy checked 55 source files. The skipped coverage includes private
operator-supplied print fixtures deliberately excluded from the public tree.

[CI run and test reports](https://github.com/thereprocase/bambu-bridge/actions/runs/34414528891)
cover the release implementation at commit 0cfe288c1d15658826820953833afa1674a90fbf.
The subsequent release commit updates validation/package documentation only.

The suite exercises local simulated MQTT/TLS, FTPS, camera services and the HTTP
API. Regression coverage includes Uvicorn/structured credential redaction, terminal
job cleanup, bounded transfers, cache replacement and directory identity,
representation-specific content ETags, Unicode downloads, printer update validation,
long-outage reconnects, and restored status/HMS context. The expected certificate
fingerprint remains internal to preserve the public snapshot contract.

The FTPS mock uses the production TLS 1.2 constraint. Generic TLS 1.3 shutdown
behavior is not the P1S protocol being tested. No real printer was heated, moved,
or sent a print by these checks; they do not establish firmware compatibility.

Source, wheel, source archive, and Home Assistant integration/add-on packaging
checks passed. The add-on source is synchronized with the root implementation.
Required license notices and corresponding-source links are included.

The public-export audit hashes selected files and scans text, comments,
configuration, identity strings, URLs and fixtures. Gitleaks archive/decoded
inspection found no credentials. Reviewed address literals are test/documentation
values or firmware versions. Deployment history, environment files, private
captures and databases remain excluded; full-file scanning cannot prove zero
residual PII.

The G-code pause mapping was cross-checked against ha-bambulab's device_error entry
03008013 in catalog blob 295dfd7837c708c44a1d6369bc6eddfdc8a95929. Other firmware-specific
HMS interpretations still need hardware validation. See THIRD_PARTY.md.
