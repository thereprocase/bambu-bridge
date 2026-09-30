"""Disposable bounded geometry worker, invoked only by PreviewSources."""

from __future__ import annotations

import hashlib
import json
import resource
import sys
from dataclasses import asdict
from pathlib import Path

from bambu_bridge.preview_assets import GeometryAssets
from bambu_bridge.source_assets import MAX_SOURCE_BYTES


def main() -> None:
    # A malformed/huge file gets a visible preview error; it cannot exhaust
    # the bridge's heap or occupy its source worker indefinitely.
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (100, 100))
    directory, filename, plate = sys.argv[1:]
    with Path(filename).open("rb") as f:
        data = f.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES or hashlib.sha256(data).hexdigest() != Path(filename).name:
        raise ValueError("Preview source integrity failed")
    shape = GeometryAssets(Path(directory)).load(data, int(plate))
    print(json.dumps(asdict(shape) if shape else None, separators=(",", ":")))


if __name__ == "__main__":
    main()
