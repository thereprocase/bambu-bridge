# Releasing Bambu Bridge

The original development history contains private operational material. Never
push that history to a public remote. The first public release must be a reviewed
source snapshot with a fresh Git history. Later releases should build on that
public history and import only reviewed source changes.

## Release checks

1. Confirm the release license and retain `LICENSE`, `NOTICE`, `LICENSES/` and
   `THIRD_PARTY.md`. The maintainer selected AGPL-3.0-only for
   the complete 0.1.1 source release.
2. Run `make test` and `make lint`. Record the actual environment, results and
   skips in `VALIDATION.md`. Mock tests do not establish printer compatibility.
3. Update the version, changelog and corresponding-source links described below.
4. Run `make build verify-dist`, `make addon-vendor addon-vendor-check`, and
   `make integration-zip`. Build the vendored add-on project separately to verify
   its self-contained packaging.
5. Inspect the selected source files and archive members, including decoded text,
   binary metadata, identity strings, URLs, test fixtures and license notices.
   Run a credential scanner over both the source export and unpacked artifacts.
   The lightweight filename/sentinel guards in the Makefile and export script
   are supplemental checks; they do not prove the absence of secrets or PII.
6. Verify the exact staged file manifest and author metadata before publishing.
   Exclude development history, private captures, printer credentials, local
   environments, caches, bytecode and operator-generated print files.

## Versions and corresponding source

`make bump-version VERSION=x.y.z` updates the four package version fields:
`pyproject.toml`, `src/bambu_bridge/__init__.py`, the add-on `config.yaml`, and the
integration `manifest.json`. Also update `SPA_BUILD` in
`src/bambu_bridge/static/app/settings.js` and refresh `uv.lock`.

For an AGPL release, update the source links in Settings/About and
`src/bambu_bridge/static/viewer.html` to the tag or commit of the code being
distributed. Create and push that tag before making an installed release
available. A modified deployment must provide its own corresponding source;
an unchanged upstream link does not cover local modifications.

## Home Assistant packages

The Supervisor builds with `homeassistant/addon/bambu-bridge/` as its Docker
context. Run `make addon-vendor` after changing source, browser assets, metadata
or license notices, and include the resulting `bridge/` source tree in the
reviewed release. The sync script excludes bytecode and compares file contents,
including additions and removals, when run with `--check`.

The integration ZIP contains `custom_components/bambu_bridge/` plus license and
provenance notices. Check those notices in both the ZIP and Python packages.
Install only on an explicitly selected test instance; packaging review alone
does not verify a live Home Assistant installation.

## Publication

Publish the reviewed snapshot to `thereprocase/bambu-bridge`, tag the matching
release, and enable GitHub Pages from `main:/docs`. Check the public source links,
site assets and installation links. Add its categorized project entry to the
main directory only once the repository is public. Keep the other paused
projects private unless separately authorized.

Release artifacts are the source tarball, wheel and Home Assistant integration
ZIP in `dist/`. Upload only individually reviewed files. Preserve the private
audit evidence locally; it is not part of the public repository or release.
