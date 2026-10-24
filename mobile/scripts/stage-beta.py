#!/usr/bin/env python3
"""Stage an APK only after signer, version and architecture checks.

This verifies packaging metadata, not feature qualification or build provenance.
No signing password/private key is read, and nothing is published or installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def inspect_apk(apk: Path, tools: Path) -> dict:
    signed = subprocess.run(
        [str(tools / "apksigner"), "verify", "--print-certs", str(apk)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    badging = subprocess.run(
        [str(tools / "aapt"), "dump", "badging", str(apk)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    package = re.search(
        r"^package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'",
        badging,
        re.M,
    )
    abi_line = re.search(r"^native-code: (.*)$", badging, re.M)
    signers = re.findall(
        r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]+)$", signed, re.M
    )
    if package is None or abi_line is None or len(signers) != 1:
        raise ValueError(
            "APK package, architecture or single-signer metadata is missing"
        )
    with apk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "package": package[1],
        "version_code": int(package[2]),
        "version_name": package[3],
        "abis": re.findall(r"'([^']+)'", abi_line[1]),
        "signer_sha256": signers[0].lower(),
        "sha256": digest,
        "bytes": apk.stat().st_size,
    }


def validate_metadata(
    metadata: dict, *, signer: str, version: str, baseline_code: int
) -> None:
    if baseline_code < 1:
        raise ValueError("installed baseline version code must be positive")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", signer):
        raise ValueError("expected signer must be a SHA-256 certificate digest")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("expected beta version is invalid")
    if metadata["package"] != "com.anonymous.bambubridgeapp":
        raise ValueError("APK package differs from the existing installed app identity")
    if metadata["signer_sha256"] != signer.lower():
        raise ValueError("APK signer differs from the existing release signer")
    if metadata["version_code"] <= baseline_code or metadata["version_name"] != version:
        raise ValueError(
            "APK version must match the beta and exceed the installed version code"
        )
    if metadata["abis"] != ["arm64-v8a"]:
        raise ValueError(
            "phone beta must contain only the qualified ARM64 architecture"
        )


def validate_version_sources(repo: Path, metadata: dict) -> None:
    package = json.loads((repo / "package.json").read_text())
    expo = json.loads((repo / "app.json").read_text())["expo"]
    lock = json.loads((repo / "package-lock.json").read_text())
    gradle = (repo / "android/app/build.gradle").read_text()
    name = re.search(r"versionName\s+[\"\']([^\"\']+)[\"\']", gradle)
    code = re.search(r"versionCode\s+(\d+)", gradle)
    versions = [
        package.get("version"),
        expo.get("version"),
        lock.get("version"),
        lock.get("packages", {}).get("", {}).get("version"),
        name[1] if name else None,
    ]
    if (
        any(v != metadata["version_name"] for v in versions)
        or code is None
        or int(code[1]) != metadata["version_code"]
    ):
        raise ValueError("source version files do not match the staged APK")
    if (
        "versionCode" in expo.get("android", {})
        and expo["android"]["versionCode"] != metadata["version_code"]
    ):
        raise ValueError("Expo Android version code differs from the APK")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--build-tools", type=Path, required=True)
    parser.add_argument("--expected-signer-sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--baseline-version-code", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    metadata = inspect_apk(args.apk, args.build_tools)
    validate_metadata(
        metadata,
        signer=args.expected_signer_sha256,
        version=args.version,
        baseline_code=args.baseline_version_code,
    )
    repo = Path(__file__).resolve().parents[1]
    validate_version_sources(repo, metadata)
    if subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo, text=True
    ).strip():
        raise ValueError("commit or isolate local changes before staging")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    # Exclusive directory creation prevents replacing an existing staged release.
    args.out.mkdir(parents=True, exist_ok=False)
    incomplete = args.out / ".incomplete"
    incomplete.write_text("Packaging verification is not complete.\n")
    target = args.out / f"bambu-bridge-{args.version}-arm64.apk"
    shutil.copyfile(args.apk, target)
    copied = inspect_apk(target, args.build_tools)
    if copied != metadata:
        raise ValueError(
            "APK changed during staging; incomplete directory retained for inspection"
        )
    manifest = {
        **metadata,
        "apk": target.name,
        "repository_head_at_staging": head,
        "verification_scope": "signature, package, version, architecture and copied bytes",
        "build_provenance": "not established by this script",
        "feature_acceptance": "requires the separate beta acceptance evidence",
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.out / "SHA256SUMS").write_text(f"{metadata['sha256']}  {target.name}\n")
    incomplete.unlink()
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"APK staging rejected: {exc}") from None
