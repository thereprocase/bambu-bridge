#!/usr/bin/env python3
"""Report, and with --apply remove, release directories and deploy snapshots nothing needs.

Usage:
    release-prune.py [--root DIR] [--unit NAME] [--keep N]
                     [--backups-dir DIR] [--backups-keep N] [--apply]

Releases under --root (default /opt/bambu-bridge-releases) are kept when they are:
  * referenced by the unit's effective WorkingDirectory, ExecStart or Environment,
    or by its EnvironmentFiles (e.g. BRIDGE_MEDIAMTX_PATH in another release);
  * referenced by the newest --keep active drop-ins, so removing the newest
    drop-in (the rollback pattern) still lands on an installed release;
  * among the --keep most recently modified release directories;
  * reachable from any kept release through a symlink, pyvenv.cfg home/command,
    a .pth line or a bin/ shebang. A release whose .venv links into an older
    release keeps that older release, and so on transitively.

Deploy snapshots in --backups-dir (default /var/lib/bambu-bridge/backups) keep
the newest --backups-keep entries. jobs-* (backup.sh) and bridge-state-*
(state-backup.sh) have their own retention and are never touched here.

Without --apply nothing is deleted: the report lists what would go and why the
rest stays. Defaults come from BRIDGE_RELEASES_KEEP (3) and
BRIDGE_DEPLOY_BACKUPS_KEEP (10). Python standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

OWN_RETENTION = ("jobs-", "bridge-state-")
_PATH_TOKEN = re.compile(r"(/[^\s'\";:=]+)")


def release_of(path: str | Path, root: Path) -> str | None:
    """Name of the release directory under root that path points into, if any."""
    try:
        relative = Path(os.path.normpath(path)).relative_to(root)
    except ValueError:
        return None
    return relative.parts[0] if relative.parts else None


def paths_in(text: str) -> list[str]:
    return _PATH_TOKEN.findall(text)


def references(directory: Path, root: Path) -> set[str]:
    """Releases this release depends on through links, venv metadata or shebangs."""
    found: set[str] = set()

    def note(target: str | Path, base: Path) -> None:
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = base / candidate
        name = release_of(candidate, root)
        if name is not None:
            found.add(name)

    for current, dirs, files in os.walk(directory, followlinks=False):
        here = Path(current)
        for name in dirs + files:
            path = here / name
            if path.is_symlink():
                note(os.readlink(path), here)
        for name in files:
            path = here / name
            if path.is_symlink():
                continue
            if name == "pyvenv.cfg" or name.endswith(".pth"):
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    continue
                for token in paths_in(text):
                    note(token, here)
            elif here.name == "bin" and here.parent.name.endswith("venv"):
                try:
                    with path.open("rb") as handle:
                        first = handle.readline(4096)
                except OSError:
                    continue
                if first.startswith(b"#!"):
                    for token in paths_in(first[2:].decode(errors="replace")):
                        note(token, here)
    found.discard(directory.name)
    return found


def closure(roots: set[str], root: Path) -> dict[str, str]:
    """Every release reachable from roots, mapped to the release that pulled it in."""
    kept = {name: "root" for name in roots if (root / name).is_dir()}
    queue = list(kept)
    while queue:
        name = queue.pop()
        for dependency in sorted(references(root / name, root)):
            if dependency not in kept and (root / dependency).is_dir():
                kept[dependency] = f"referenced by {name}"
                queue.append(dependency)
    return kept


def unit_state(unit: str) -> tuple[str, list[Path]]:
    """Effective unit settings as text, and its active drop-in files in apply order."""
    shown = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "WorkingDirectory",
            "-p",
            "ExecStart",
            "-p",
            "Environment",
            "-p",
            "EnvironmentFiles",
            "-p",
            "DropInPaths",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    text = shown
    dropins: list[Path] = []
    for line in shown.splitlines():
        key, _, value = line.partition("=")
        if key == "EnvironmentFiles":
            for token in value.split():
                if token.startswith("/"):
                    with contextlib.suppress(OSError):
                        text += "\n" + Path(token).read_text(errors="replace")
        elif key == "DropInPaths":
            dropins = [Path(token) for token in value.split() if token.endswith(".conf")]
    return text, dropins


def plan_releases(
    root: Path, keep: int, unit_text: str, dropins: list[Path]
) -> tuple[dict[str, str], list[str]]:
    releases = sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    reasons: dict[str, str] = {}
    for token in paths_in(unit_text):
        name = release_of(token, root)
        if name is not None:
            reasons.setdefault(name, "live unit")
    for dropin in dropins[-keep:] if keep else []:
        try:
            text = dropin.read_text(errors="replace")
        except OSError:
            continue
        for token in paths_in(text):
            name = release_of(token, root)
            if name is not None:
                reasons.setdefault(name, f"drop-in {dropin.name}")
    for path in releases[:keep]:
        reasons.setdefault(path.name, "newest")
    kept = closure(set(reasons), root)
    for name, why in kept.items():
        if why == "root":
            kept[name] = reasons[name]
    remove = [p.name for p in releases if p.name not in kept]
    return kept, remove


def plan_backups(directory: Path, keep: int) -> list[Path]:
    if not directory.is_dir():
        return []
    candidates = [
        p
        for p in directory.iterdir()
        if not p.name.startswith(OWN_RETENTION) and not p.is_symlink()
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[keep:] if keep else []


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for current, _, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(current) / name).lstat().st_size
            except OSError:
                continue
    return total


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path("/opt/bambu-bridge-releases"))
    parser.add_argument("--unit", default="bambu-bridge")
    parser.add_argument("--keep", type=int, default=int(os.environ.get("BRIDGE_RELEASES_KEEP", 3)))
    parser.add_argument("--backups-dir", type=Path, default=Path("/var/lib/bambu-bridge/backups"))
    parser.add_argument(
        "--backups-keep",
        type=int,
        default=int(os.environ.get("BRIDGE_DEPLOY_BACKUPS_KEEP", 10)),
    )
    parser.add_argument("--apply", action="store_true", help="delete; default is a report")
    parser.add_argument("--unit-text", type=Path, help=argparse.SUPPRESS)  # tests
    parser.add_argument("--dropin", type=Path, action="append", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be at least 1")

    if args.unit_text is not None:
        unit_text, dropins = args.unit_text.read_text(), list(args.dropin or [])
    else:
        unit_text, dropins = unit_state(args.unit)
    live = {release_of(t, args.root) for t in paths_in(unit_text)} - {None}
    if not live and args.root.is_dir():
        print("release-prune: the unit references no release under the root; refusing")
        return 2

    kept, releases = plan_releases(args.root, args.keep, unit_text, dropins)
    backups = plan_backups(args.backups_dir, args.backups_keep)
    report = {
        "applied": args.apply,
        "kept_releases": dict(sorted(kept.items())),
        "remove_releases": {name: size_of(args.root / name) for name in releases},
        "remove_backups": {p.name: size_of(p) for p in backups},
    }
    if args.apply:
        for name in releases:
            remove(args.root / name)
        for path in backups:
            remove(path)
    total = sum(report["remove_releases"].values()) + sum(report["remove_backups"].values())
    report["bytes"] = total
    json.dump(report, sys.stdout, indent=2)
    print()
    verb = "removed" if args.apply else "would remove"
    print(
        f"release-prune: {verb} {len(releases)} releases and {len(backups)} deploy "
        f"snapshots, {total / 1e6:.1f} MB; kept {len(kept)} releases",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
