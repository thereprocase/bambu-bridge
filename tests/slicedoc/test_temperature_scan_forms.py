"""F05/F28: the temperature scan reads every legal S-word form; corrupt archives are issues."""

from __future__ import annotations

import io
import struct
import zipfile

import pytest

from bambu_bridge.slicedoc import scan_gcode, validate

# (line, kind, expected °C) — each form below slipped past the old exact `M104 S<n>` match.
_FORMS = [
    (b"m104 s350", "nozzle", 350),
    (b"N12 M104 S350", "nozzle", 350),
    (b"M104 T0 S350", "nozzle", 350),
    (b"M109 P0 S350", "nozzle", 350),
    (b"M104 S 350", "nozzle", 350),
    (b"M104 S+350", "nozzle", 350),
    (b"M104 S3.5e2", "nozzle", 350),
    (b"M104 T0S350", "nozzle", 350),
    (b"M104S350", "nozzle", 350),
    (b"M140 S 130", "bed", 130),
    (b"n3 m190 s+1.3E2", "bed", 130),
    (b"M190 R50 S130", "bed", 130),
]


@pytest.mark.parametrize(("line", "kind", "value"), _FORMS)
def test_scan_reads_s_word_forms(line: bytes, kind: str, value: float) -> None:
    scan = scan_gcode(b"G28\n" + line + b"\nG1 X1\n")
    assert (scan.max_nozzle_c if kind == "nozzle" else scan.max_bed_c) == value
    assert (scan.max_bed_c if kind == "nozzle" else scan.max_nozzle_c) is None


def test_scan_ignores_comment_text() -> None:
    scan = scan_gcode(b"M104 S200 ; was S350\n; M104 S999\nM140 S60 ;M190 S130\n")
    assert scan.max_nozzle_c == 200
    assert scan.max_bed_c == 60


def test_scan_does_not_confuse_longer_commands() -> None:
    scan = scan_gcode(b"M1040 S999\nM1900 S999\nM104.1 S1\n")
    assert scan.max_nozzle_c in (None, 1)
    assert scan.max_bed_c is None


@pytest.mark.parametrize(
    ("line", "issue"),
    [(b"N5 M104 T0 S3.5e2", "G4 nozzle 350"), (b"m190 s 130", "G4 bed 130")],
)
def test_disguised_overtemperature_reaches_the_limit_check(line: bytes, issue: str) -> None:
    from tests.slicedoc.test_validate_source_agnostic import _BIND0, _SINGLE_PETG, _container

    report = validate(_container(_SINGLE_PETG, line + b"\n" + _BIND0))
    assert any(issue in i for i in report.issues), report.issues


def _archive(compress_type: int) -> bytearray:
    from tests.slicedoc.test_validate_source_agnostic import _BIND0, _SINGLE_PETG, _md5

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode", b"G28\n" * 200 + _BIND0, compress_type=compress_type)
        z.writestr("Metadata/plate_1.gcode.md5", _md5(b"x"))
        z.writestr("Metadata/slice_info.config", _SINGLE_PETG)
    return bytearray(buf.getvalue())


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_corrupt_compressed_member_is_an_issue_not_an_exception(method: int) -> None:
    raw = _archive(method)
    name_len, extra_len = struct.unpack_from("<HH", raw, 26)
    start = 30 + name_len + extra_len  # first member's compressed stream
    raw[start + 12 : start + 40] = b"\xff" * 28
    report = validate(bytes(raw))
    assert any(i.startswith("G1") for i in report.issues), report.issues


def test_non_utf8_member_name_is_an_issue() -> None:
    raw = _archive(zipfile.ZIP_STORED)
    raw[7] |= 0x08  # local header: name is UTF-8 (bit 11)
    cd = raw.index(b"PK\x01\x02")
    raw[cd + 9] |= 0x08  # central directory entry
    raw[raw.index(b"Metadata")] = 0xFF
    raw[raw.index(b"Metadata", cd)] = 0xFF
    report = validate(bytes(raw))
    assert any(i.startswith("G1") for i in report.issues), report.issues
