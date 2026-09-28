# Releases

1. Run bridge and app tests, privacy checks and a secret scanner.
2. Review the staged source, binary assets and license notices.
3. Build the Python distribution with `uv build`.
4. Build/sign Android using a private keystore; keep signing credentials local.
5. Build the Home Assistant bundle with `make addon-vendor` before packaging
   its add-on directory. The generated copy is excluded from version control.
6. Publish reviewed artifacts and checksums from the corresponding source tag.

The Home Assistant add-on requires the generated `bridge/` directory in its
build context. A plain source checkout must run the vendor step before an
add-on build; use a prepared release bundle for installation.
