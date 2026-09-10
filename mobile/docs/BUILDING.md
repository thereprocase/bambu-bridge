# Building Bambu Bridge — Android Release APK

## Prerequisites

- JDK 17 (including `javac`)
- Android SDK with build-tools matching `android/build.gradle` (`buildToolsVersion`)
- Node 22 and the project's `node_modules` already installed (`npm ci` once, offline thereafter)
- A release keystore at `android/app/release.keystore` (gitignored — never commit it)

## Signing credentials

Credentials are **never** stored in source. Supply them via one of:

**Option A — `~/.gradle/gradle.properties`** (recommended for local builds, untracked):

```
RELEASE_STORE_PASSWORD=<your keystore password>
RELEASE_KEY_ALIAS=bambu-bridge
RELEASE_KEY_PASSWORD=<your key password>
```

**Option B — environment variables** (CI or shell):

```sh
export RELEASE_STORE_PASSWORD='<your keystore password>'
export RELEASE_KEY_ALIAS=bambu-bridge
export RELEASE_KEY_PASSWORD='<your key password>'
```

`android/app/build.gradle` reads `findProperty(...)` first, then `System.getenv(...)`.
The keystore file `android/app/release.keystore` must be present on disk but is gitignored.

## Building a signed release APK

```sh
cd /path/to/bambu-bridge-app/android
./gradlew assembleRelease
```

> **JDK, not JRE.** Use a real JDK 17 (one that has `javac`). Gradle's toolchain
> auto-detection can grab a newer JRE-only install (e.g. a `java-21` JRE) and fail
> with `Toolchain installation ... does not provide the required capabilities:
> [JAVA_COMPILER]`. Point the build at a JDK 17 explicitly:
>
> ```sh
> JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ./gradlew assembleRelease
> ```
>
> (add `-Dorg.gradle.java.installations.auto-detect=false` if a stray JRE keeps
> getting picked).

Output lands at:

```
android/app/build/outputs/apk/release/app-release.apk
```

Copy or rename it before distributing:

```sh
cp android/app/build/outputs/apk/release/app-release.apk \
   bambu-bridge-v$(node -p "require('./package.json').version").apk
```

## Version-bump checklist

All three sources must be updated together on every release:

| File | Field | Example |
|------|-------|---------|
| `android/app/build.gradle` | `versionName` | `"0.18.3"` |
| `android/app/build.gradle` | `versionCode` | `22` (monotonically increasing integer — never reuse) |
| `app.json` | `expo.version` | `"0.18.3"` |
| `package.json` | `version` | `"0.18.3"` |

`build.gradle` is the canonical source for Android identity. `app.json` and `package.json`
mirror it. Keep all three in sync before cutting a release.

## Distribution

Releases are distributed as GitHub Release assets (no Play Store).
Attach the signed APK to a GitHub Release tagged `v<versionName>` (e.g. `v0.18.3`).
No Play Store review process is in scope for this project.

## Debug builds

```sh
cd android && ./gradlew assembleDebug
```

The Gradle file expects the standard debug keystore at `android/app/debug.keystore`.
Create it locally if needed (these are the standard non-secret debug credentials):

```sh
keytool -genkeypair -keystore android/app/debug.keystore -storepass android \
  -alias androiddebugkey -keypass android -dname "CN=Android Debug,O=Android,C=US" \
  -keyalg RSA -keysize 2048 -validity 10000
```

A debug build cannot update an installed release build with a different signer.
Use an emulator or a separate application ID when developing alongside your installed app.

## Proguard / R8

Minification is off by default (`android.enableMinifyInReleaseBuilds=false`).
Enable it by passing `-Pandroid.enableMinifyInReleaseBuilds=true` to gradlew
or adding the property to `~/.gradle/gradle.properties`. Test thoroughly
before shipping a minified build — the JS bundle runs on Hermes, but native
glue code is subject to shrinking.

## Reproducing the v0.18.3 checks

```sh
npx tsc --noEmit
npm test -- --runInBand
cd android
./gradlew assembleRelease --no-daemon --max-workers=2 -PreactNativeArchitectures=arm64-v8a
```

The released APK was signed locally. Create your own release keystore with
`keytool -genkeypair -keystore android/app/release.keystore -alias bambu-bridge -keyalg RSA -keysize 3072 -validity 10000`.
Keep it outside version control. A build signed with your key cannot replace
the project's installed release while retaining that app's data.

`package-lock.json` pins JavaScript dependencies. Keep its root version in
sync with the three version files above. No signing material is supplied.

## Network policy

The bridge URL is chosen by the user. HTTP is enabled to support local servers
addressed by IP as well as private DNS names. The Android configuration does
not enforce private IP ranges. Use HTTPS or a trusted VPN across an untrusted
network. HTTPS uses the system CA store; certificate checks are not disabled.
See [Android's network security configuration](https://developer.android.com/privacy-and-security/security-config).
