"""JPEG scan-complexity parsing — the zero-dep spaghetti signal.

We never decode pixels; we isolate the entropy-coded scan and score it.
These lock the marker-walk: stuffing/restart stay *inside* the scan, real
markers end it, and anything unparseable is ``None`` (never a false signal).
"""

from __future__ import annotations

import pytest

from bambu_bridge.vision.frame import scan_complexity


def _jpeg(scan: bytes, *, trailing_marker: bytes = b"\xff\xd9") -> bytes:
    """Minimal baseline JPEG: SOI + a DQT segment + SOS + scan + EOI."""
    soi = b"\xff\xd8"
    dqt = b"\xff\xdb\x00\x04\x00\x00"  # marker + len=4 + 2 payload bytes
    sos = b"\xff\xda\x00\x03\x00"  # marker + len=3 + 1 header byte
    return soi + dqt + sos + scan + trailing_marker


def test_parses_scan_and_scores_at_least_its_length() -> None:
    scan = bytes(range(0, 200))  # 200 bytes, no FF
    fc = scan_complexity(_jpeg(scan))
    assert fc is not None
    assert fc.scan_len == 200
    assert fc.score >= fc.scan_len  # modulation factor is >= 1.0
    assert 0.0 <= fc.entropy <= 8.0


def test_stuffing_and_restart_stay_inside_the_scan() -> None:
    # FF 00 (stuffed literal) and FF D2 (restart) must NOT end the scan;
    # FF C4 (a real DHT marker) must.
    scan = b"\x11\x22\xff\x00\x33\xff\xd2\x44\x55"
    body = _jpeg(scan, trailing_marker=b"\xff\xc4\x00\x02")
    fc = scan_complexity(body)
    assert fc is not None
    assert fc.scan_len == len(scan)  # everything up to FF C4


def test_more_bytes_scores_higher() -> None:
    small = scan_complexity(_jpeg(bytes(range(50))))
    big = scan_complexity(_jpeg(bytes(range(50)) * 20))
    assert small is not None and big is not None
    assert big.score > small.score


def test_entropy_modulation_is_bounded() -> None:
    # A maximally uniform (high-entropy) scan and a flat (low-entropy) scan
    # of equal length must not differ in score by more than a 2x factor.
    n = 4096
    uniform = bytes(i % 256 for i in range(n))
    flat = b"\x42" * n
    fu = scan_complexity(_jpeg(uniform))
    ff = scan_complexity(_jpeg(flat))
    assert fu is not None and ff is not None
    assert fu.score <= 2.0 * ff.scan_len  # modulation capped at +1.0


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"\xff\xd8",  # SOI only, no SOS
        b"not a jpeg at all",
        b"\x89PNG\r\n\x1a\n",  # PNG signature
        b"\xff\xd8\xff\xdb\x00\x04\x00\x00",  # segment but never an SOS
        b"\xff\xd8\xff\xda\x00\x03\x00",  # SOS but empty scan + no EOI
    ],
)
def test_unparseable_is_none_never_raises(blob: bytes) -> None:
    assert scan_complexity(blob) is None
