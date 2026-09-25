# Probe evidence

`windows-nightly-20260914-empty-bed.json` was written by the diagnostic page
inside stock Orca's Python runtime on Windows, using a separate empty test
profile. The Library page and probe capability both logged successful loading.
This is runtime API inspection, not a populated-plate or capture acceptance test.

Official asset: `OrcaSlicer_Windows_x64_nightly_portable.zip`, downloaded
2026-09-14 from the OrcaSlicer/OrcaSlicer GitHub nightly release.

SHA-256: `1d9c42e0121e713700a1febca81447373ddb9d808061e36549c5549ac0263d71`

Observed version: `2.5.0-dev`. Separately inspected source revision:
`292cf0095e698a6e0f96041bd142fd41afd6ccfb`. The runtime Plater API agrees with
those source bindings. The asset's exact build revision was not independently
established from its binary.

The report omits source paths, object names, profiles and credentials. The
installed stable 2.4.2 profile and active printer were not part of this test.
