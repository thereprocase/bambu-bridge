# Validation — 0.1.2, 9 September 2026

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
