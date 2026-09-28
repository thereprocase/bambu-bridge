# Bambu Bridge Android

The primary owner interface for the bridge in the parent directory. Features
include printer status, camera viewing, print files, bridge queue recovery,
filament information, and model-qualified controls.

- [Install and pair](docs/INSTALL.md)
- [Build instructions](docs/BUILDING.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

Use Node.js 22, Java 17 and the Android SDK. From this directory:

```sh
npm ci
npm test -- --runInBand
npx tsc --noEmit
npm run lint
cd android
./gradlew testDebugUnitTest assembleDebug
```

Create a local debug keystore as described in the build guide. Release signing
uses an operator-supplied keystore and credentials; they are excluded from Git.
Existing application/package identifiers are retained for update compatibility.

Brand source vectors live in `assets/brand/`. Rebuild raster exports with
`node scripts/build-brand.cjs` using librsvg and ImageMagick.

AGPL-3.0-only; third-party notices are in [THIRD_PARTY.md](THIRD_PARTY.md).
