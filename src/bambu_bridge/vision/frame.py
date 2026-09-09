"""Complexity of a baseline-JPEG chamber frame, with **zero** image deps.

We cannot decode pixels (no numpy/PIL/cv2 by design), so we measure the
*entropy-coded scan* instead — the Huffman-coded segment after ``SOS`` that
holds the actual DCT energy. Two facts make this a usable spaghetti proxy:

* Scan **length** scales with scene detail. An empty plate / clean hull is
  smooth and low-AC; it compresses tiny. Filament spaghetti is a dense mat
  of high-frequency edges — many large AC coefficients — and the scan
  balloons. (We literally watched the *file* go 41 KB clean -> chaotic.)
* Scan byte **entropy** rises slightly with that complexity too. It is a
  weaker signal (Huffman output is already near-random ~7.5+ bits/byte) so
  it only nudges the score; length carries it.

We combine them into one scalar so the detector tracks a single series.

This is parsed defensively: the P1S camera emits baseline (single-scan,
non-progressive) JPEG, but a torn/half frame must yield ``None`` ("no
signal" — never a false trigger), never an exception.

JPEG marker facts used (ITU-T T.81): segments are ``FF <marker>``; ``FF 00``
inside the scan is a stuffed literal 0xFF; ``FF D0..D7`` are restart markers
*within* a scan; ``FF D9`` is EOI. Markers ``FF D0-D7``/``D8``/``D9``/``01``
carry no length; everything else carries a 2-byte big-endian length.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Markers that do NOT carry a 2-byte length field.
_STANDALONE = {0x01, *range(0xD0, 0xDA)}  # TEM, RST0-7, SOI(D8), EOI(D9)
_SOI = 0xD8
_EOI = 0xD9
_SOS = 0xDA


@dataclass(frozen=True, slots=True)
class FrameComplexity:
    """One frame's parsed complexity.

    ``score`` is the value the detector tracks; ``scan_len`` / ``entropy``
    are kept for logging and tests so a trigger can be explained.
    """

    score: float
    scan_len: int
    entropy: float


def _byte_entropy(buf: bytes) -> float:
    """Shannon entropy of the byte histogram, in bits/byte (0..8)."""
    if not buf:
        return 0.0
    counts = [0] * 256
    for b in buf:
        counts[b] += 1
    n = len(buf)
    h = 0.0
    for c in counts:
        if c:
            p = c / n
            h -= p * math.log2(p)
    return h


def _find_scan(jpeg: bytes) -> bytes | None:
    """Return the entropy-coded bytes between ``SOS`` and the next marker.

    None if the structure is not a parseable baseline JPEG.
    """
    n = len(jpeg)
    if n < 4 or jpeg[0] != 0xFF or jpeg[1] != _SOI:
        return None
    i = 2
    while i + 1 < n:
        if jpeg[i] != 0xFF:
            return None  # desync — not at a marker boundary
        # Skip fill bytes (0xFF padding before a marker).
        marker = jpeg[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in _STANDALONE:
            i += 2
            continue
        if i + 3 >= n:
            return None
        seg_len = (jpeg[i + 2] << 8) | jpeg[i + 3]
        if seg_len < 2:
            return None
        if marker == _SOS:
            scan_start = i + 2 + seg_len
            return _scan_until_marker(jpeg, scan_start)
        i += 2 + seg_len
    return None


def _scan_until_marker(jpeg: bytes, start: int) -> bytes | None:
    """Entropy-coded run from ``start`` to EOI / the next real marker.

    ``FF 00`` (byte stuffing) and ``FF D0-D7`` (restart) stay *inside* the
    scan; any other ``FF xx`` ends it.
    """
    n = len(jpeg)
    if start >= n:
        return None
    i = start
    while i + 1 < n:
        if jpeg[i] == 0xFF:
            nxt = jpeg[i + 1]
            if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                i += 2
                continue
            break  # EOI or next segment marker
        i += 1
    scan = jpeg[start:i]
    return scan if scan else None


def scan_complexity(jpeg: bytes) -> FrameComplexity | None:
    """Parse one JPEG frame to a single complexity scalar.

    Returns ``None`` for anything we cannot parse (truncated frame, not
    baseline JPEG) — the detector treats that as a skipped observation, so a
    mangled frame can never *cause* a trigger.

    ``score = scan_len * (1 + (entropy - 7) clamped to [0, 1])`` — length is
    the spine; entropy only modulates it within a bounded factor so a single
    near-random scan can't dominate.
    """
    scan = _find_scan(jpeg)
    if scan is None:
        return None
    scan_len = len(scan)
    entropy = _byte_entropy(scan)
    modulation = 1.0 + max(0.0, min(1.0, entropy - 7.0))
    return FrameComplexity(
        score=scan_len * modulation,
        scan_len=scan_len,
        entropy=entropy,
    )
