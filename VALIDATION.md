# Validation — 0.1.2 candidate, 9 September 2026

The 23 focused regression/HMS tests pass. Ruff passes, and strict mypy reports
no issues in 55 source files. The broader focused API/cache run passed 122 of
123 tests; its one failure identified the old test that mislabeled a G-code
pause as external-spool runout. That assertion and its remediation were corrected
against the reviewed community catalog and pass in the final regression run.

The complete simulated MQTT/TLS, FTPS, camera, and API suite is running locally.
The candidate branch is also submitted to the Python 3.12/3.13 GitHub workflow.
Final suite, packaging, and deployment results will replace this candidate record
before the v0.1.2 release is published.

The source scan includes comments, configuration, identity strings, URLs, and
fixtures. The reviewed IP strings are synthetic test/documentation addresses or
firmware versions; credential literals are test placeholders. Gitleaks with
archive/decoded inspection reports no credentials. The imported runtime changes
exclude deployment history, environment files, private captures, and databases.

The restored HMS/stage guidance is not an authoritative vendor catalog. The
G-code pause mapping was cross-checked against ha-bambulab's device_error entry
03008013 in catalog blob 295dfd7837c708c44a1d6369bc6eddfdc8a95929. Other firmware-specific
interpretations still require hardware validation; see THIRD_PARTY.md.
