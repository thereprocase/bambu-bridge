# Validation — 9 September 2026

This is a source release. The full Linux mock-based suite completed
with **838 passing tests and nine skips**. Ruff passed, and strict mypy reported
no issues in 53 source files. The skipped coverage includes operator-supplied
print fixtures that are deliberately excluded from the public source tree.

The suite exercised local simulated MQTT/TLS, FTPS and camera services and the
HTTP API; no real printer was connected, heated, moved or sent a job during this
review. These checks do not establish current firmware compatibility or validate
the five HMS code meanings. See `THIRD_PARTY.md` for the provenance limits.

The candidate adds license notices, source links, public documentation and
packaging metadata to that implementation. Distribution contents, source-link
markup, metadata and syntax are checked separately after packaging. Hardware
acceptance and a new browser viewport sweep remain outside this source review.

The public-export process hashes every selected file and scans text, comments,
configuration and embedded strings for identities and credentials. Original
history, private operator captures, printer certificates and camera images stay
outside the export. Required public license and author notices are retained.
Full-file scanning and manual findings review cannot prove zero residual PII.
