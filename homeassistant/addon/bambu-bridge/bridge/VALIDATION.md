# Native camera keyframe correction - 0.4.2

The 0.4.1 byte-level camera checks received valid JPEGs but did not invoke
Orca's native stream parser. The installed camera DLL reproduced the failure:
frames arrived, were treated as non-keyframes, and stream startup stalled.
Physical P1S headers use four little-endian words: payload length, zero, one,
zero. The gateway had emitted zero for the third word.

A loopback-only fixture used a generated 640 x 480 JPEG and dummy credentials
with the installed Windows BambuSource.dll, through the C interface described
in OrcaSlicer's BambuTunnel.h. With the old flag, startup still returned
would-block after 25 seconds. Changing only the third word to one returned
successful stream startup, one stream and a 5,429-byte JPEG in 0.2 seconds.
No vendor binaries, printer credentials or chamber images are redistributed.

The native regression now checks all four header words, including the
keyframe flag. Full CI and subsequent live checks are recorded in the release
notes. A camera-library check does not establish UI playback or a completed
physical print.

# Separate native printer identity - 0.4.1

A v0.4.0 setup completed, but subsequent Orca LAN discovery replaced
the same-serial entry with the physical printer's name and address. Orca's
DeviceCore/DevManager.cpp indexes those updates by device ID. The fix gives the
bridge a separate persistent identity throughout native discovery, detection,
TLS, MQTT and owner-generated setup. DeviceManager.cpp also saves access-code
query replies; those are now answered locally or transposed before delivery.

All 29 focused native/owner tests pass, including distinct/persistent identity,
certificate binding, rejection of physical-serial topics, native password
queries, unsolicited upstream credential reports, unchanged upstream snapshots,
and reverse identity translation without changing file or AMS parameters.
The shipped Windows helper is exercised against two-printer fixtures: the
physical P1S entry and both code fields remain unchanged through fresh, existing
and repeated bridge setup. Previous address-isolation and owner-auth checks
remain covered. Final full CI counts are recorded in the release notes.

Protocol fixtures do not establish actual Orca UI playback or a completed print.
No physical print is started by the release checks.

# Guided Orca setup and native address isolation - 0.4.0

Local release checks on Ubuntu/WSL with Python 3.12 collected 924 tests:
914 passed and 10 skipped (the Windows helper test runs separately on Windows).
All 27 focused native/owner regressions pass. Ruff and strict mypy pass across
65 source modules. Chromium reports no JavaScript errors or horizontal overflow
at widths 320, 1440 and 3840 pixels. Release notes record the final CI results.

The existing offsite Orca connection was confirmed by the user after correcting
its saved address and adding the bridge certificate to Orca's printer bundle.
A subsequent live-view timeout exposed `print.net.info[].ip`: firmware encodes
IPv4 as a little-endian integer, which Orca consumes to overwrite its active
address. The deployed old feed was observed advertising the physical interface.
A separately authenticated Windows client received a valid native camera JPEG.

The new wire checks cover both bootstrap and incremental address transposition,
alternate interfaces and URL references, zero addresses, unrelated numbers,
input immutability, and per-computer connection isolation. Owner API checks
cover HTTPS, authentication, no-store responses and exclusion of paired phones.
Windows PowerShell fixture checks cover fresh registration and repair, repeat
runs, Unicode/nested settings, other printers, shared-code fields, exact backups,
valid checksums and pinned TLS. They leave the installed Orca profile untouched.
A dedicated Windows CI job runs the shipped helper against disposable profiles
and a local TLS endpoint. Browser checks cover guided copy, manual controls and
separate live status at mobile, desktop and 4K sizes.

These checks do not establish an actual Windows UAC interaction on every
installation, macOS/Linux automation, Orca camera playback after deployment,
or a completed physical print. No physical print was started by this release.

# Repeatable native code retrieval — 0.3.3

All 24 focused native/owner API tests pass. Coverage includes repeated HTTPS
retrieval, refusal of unauthenticated/HTTP/paired-phone requests, no-store
responses, encrypted storage, restart/rotation/disable behavior, and migration
of an older code on verified reconnect without changing its authentication hash.
Wrong codes are never saved. Ordinary status responses contain no access code.
The existing shared-code/multiple-client and native protocol tests still pass.
Chromium with an isolated HTTPS backend verifies Show/Hide/Copy, copying the
same code after navigation and refresh, rotation and disabling, with no
JavaScript errors or horizontal overflow at widths 320, 1440 and 3840 pixels.

# Native IP identification patch — 0.3.2

Orca v2.4.2 calls `bind_detect` before connecting MQTT from its initial IP/code
screen. The previously missing private TCP 3000 listener now answers that
read-only identity request. Framed-stream regression tests verify automatic
model/serial/name resolution, split TCP writes, credential exclusion, malformed
length/trailer/JSON rejection, refusal of login and print commands, and closure
of partial requests when native access is disabled. A two-client TLS test
verifies that the same native code supports simultaneous status subscriptions.
All 22 focused native and owner API tests pass in Python 3.12.

This is protocol verification. Installed Orca UI acceptance from the offsite
computer and a completed physical print remain unverified. No print was started.

# Native TLS compatibility patch — 0.3.1

Native TLS now uses a separate persistent RSA identity with the configured
private address in its certificate. Regression tests exercise a TLS 1.2 client
offering only an RSA-authenticated cipher suite, with certificate and hostname
verification enabled. Existing phone identities remain unchanged. Owner-only
connection diagnostics report protocol stages and rejected-code counts without
peer addresses, passwords or packet contents. These changes address a confirmed
RSA-only TLS compatibility gap; the reported offsite Orca failure is still under
investigation and is not claimed resolved by the fixture tests.

# Native P1S gateway preview — 0.3.0

Live protocol acceptance on 2026-09-10 verified real P1S AMS/external-spool
status, a read-only version request and response, camera JPEG framing and
dimensions, and an FTPS file listing. The HTTPS dashboard enabled and disabled
native access at 320/1440/3840 pixels with normal certificate validation and
zero JavaScript errors. Temporary native access was disabled after testing.
Existing phone pairing and server identity were preserved. No print was started.

Full CI for release commit `3fdaaa252dddf848bd3fad474efd36ad19eb4436` ran 904 tests
on each of Python 3.12 and 3.13: 895 passed, 9 skipped, zero failures or errors.
[CI evidence](https://github.com/thereprocase/bambu-bridge/actions/runs/34510538602).

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
