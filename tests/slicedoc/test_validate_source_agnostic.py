"""validate() accepts what OrcaSlicer writes and sends, and rejects the §6.3 air print.

Real fixtures (tests/fixtures/orca) are OrcaSlicer 2.4.2 CLI slices of a.stl/b.stl
with the P1S profiles and three filaments in the project:

* single1  — one filament project, cube on filament 1
* sparse3  — three filaments, cube on filament 3 only (``--load-filament-ids 3``)
* sparse13 — three filaments, cubes on filaments 1 and 3 (``--load-filament-ids 1,3``)

All three have ``printer_model_id=""`` in slice_info (the CLI never fills it).
``ams_mapping`` is Orca's v0 list: index = project filament, value = AMS tray,
-1 = unused; ``[]``/all -1 = external spool.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import zipfile
from pathlib import Path

import pytest

from bambu_bridge.slicedoc import validate

validate_module = importlib.import_module("bambu_bridge.slicedoc.validate")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "orca"


def _mk(members: dict[str, bytes] | list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    items = members.items() if isinstance(members, dict) else members
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in items:
            z.writestr(name, data)
    return buf.getvalue()


def _md5(gcode: bytes) -> bytes:
    return hashlib.md5(gcode).hexdigest().upper().encode()  # noqa: S324


def _container(slice_info: str, gcode: bytes) -> bytes:
    """Only the three members the gate reads — a real slicer's minimal shape."""
    return _mk(
        {
            "Metadata/plate_1.gcode": gcode,
            "Metadata/plate_1.gcode.md5": _md5(gcode),
            "Metadata/slice_info.config": slice_info.encode(),
        }
    )


def _info(*ids: int, model: str = "C12", nozzle: str = "0.4") -> str:
    filaments = "".join(f'<filament id="{i}" type="PETG" color="#161616"/>' for i in ids)
    return (
        '<?xml version="1.0"?><config><plate>'
        f'<metadata key="printer_model_id" value="{model}"/>'
        f'<metadata key="nozzle_diameters" value="{nozzle}"/>'
        f"{filaments}</plate></config>"
    )


_SINGLE_PETG = _info(1)
_BIND0 = b"M620 S0A\nT0\nM621 S0A\n"


# --------------------------------------------------------------------------- #
# Real OrcaSlicer output
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fixture", "mapping", "issue"),
    [
        ("single1", [0], None),
        ("single1", [3], None),  # tray value is the physical remap, not the gcode index
        ("single1", None, None),  # upload only
        ("single1", [], None),  # external spool
        ("single1", [-1], None),  # Orca's external spool form
        ("single1", [2, -1, -1, -1], None),  # longer than the project is harmless
        ("single1", [-1, 2], "[1] have no AMS tray"),
        ("sparse3", [-1, -1, 2], None),  # what Orca sends
        ("sparse3", [0, 1, 2], None),
        ("sparse3", [], None),
        ("sparse3", [2], "[3] have no AMS tray"),  # the old used-filament-count format
        ("sparse3", [0, 1, -1], "[3] have no AMS tray"),
        ("sparse13", [0, -1, 2], None),  # what Orca sends
        ("sparse13", [3, 1, 0, 2], None),
        ("sparse13", [0, 2], "[3] have no AMS tray"),
        ("sparse13", [], "external spool feeds one filament"),
        ("sparse13", [-1, -1, -1], "external spool feeds one filament"),
        ("sparse13", [16, -1, 0], "-1..15"),
    ],
)
def test_real_orca_slices(fixture: str, mapping: list[int] | None, issue: str | None) -> None:
    data = (FIXTURES / f"{fixture}.gcode.3mf").read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:  # the CLI leaves the model id empty
        assert b'"printer_model_id" value=""' in z.read("Metadata/slice_info.config")
    r = validate(data, expected_ams_mapping=mapping, expected_nozzle=0.4)
    if issue is None:
        assert r.ok, r.issues
    else:
        assert any(issue in i for i in r.issues), r.issues


def test_real_slice_for_another_nozzle_is_rejected() -> None:
    r = validate((FIXTURES / "single1.gcode.3mf").read_bytes(), expected_nozzle=0.6)
    assert r.issues == ["G6 sliced nozzle 0.4 mm differs from the printer's 0.6 mm"]


# --------------------------------------------------------------------------- #
# G5 — the §6.3 air print and its parts
# --------------------------------------------------------------------------- #


def test_air_print_is_rejected_without_the_probe() -> None:
    # The 2026-05-19 file in miniature: slice_info declares only filament 5,
    # the G-code loads S1 but finishes S0 (with Orca's indented T0), and
    # ams_mapping [1] has no entry for index 1.
    gcode = b"M620 M\nM620 S1A\n    M109 S250\n    T0\nM621 S0A\nM620 S255\nT255\nM621 S255\n"
    r = validate(_container(_info(5), gcode), expected_ams_mapping=[1])
    joined = " ".join(r.issues)
    assert "handshake incoherent: loads=[1] finishes=[0] tools=[0]" in joined
    assert "selects project filament(s) [1, 2] but slice_info declares [5]" in joined
    assert "[2] have no AMS tray in ams_mapping [1]" in joined


def test_indented_tool_select_is_part_of_the_handshake() -> None:
    r = validate(_container(_SINGLE_PETG, b"M620 S0A\n    T1\nM621 S0A\n"))
    assert any("handshake incoherent" in i for i in r.issues), r.issues


def test_sentinels_and_calibration_are_not_binds() -> None:
    gcode = (
        b"M620 M\nM620 S0A\n    T0\nM621 S0A\nM620.1 E F199 T260\nM620.11 S0\n"
        b"T1000\nM620 S255\nT255\nM621 S255\n"
    )
    assert validate(_container(_SINGLE_PETG, gcode), expected_ams_mapping=[2]).ok


def test_two_filament_slice_with_both_layers_listed() -> None:
    gcode = b"M620 S0A\nT0\nM621 S0A\nG1 X1\nM620 S1A\nT1\nM621 S1A\nM620 S0A\nT0\nM621 S0A\n"
    r = validate(_container(_info(1, 2), gcode), expected_ams_mapping=[3, 1])
    assert r.ok, r.issues


def test_gcode_using_an_undeclared_filament_is_rejected() -> None:
    r = validate(_container(_info(1), b"M620 S2A\nT2\nM621 S2A\n"))
    assert any("selects project filament(s) [3]" in i for i in r.issues), r.issues


@pytest.mark.parametrize("ids", [(), ("0",), ("x",), ("1", "")])
def test_filament_ids_must_be_project_numbers(ids: tuple[str, ...]) -> None:
    info = _info().replace("</plate>", "".join(f'<filament id="{i}"/>' for i in ids) + "</plate>")
    r = validate(_container(info, _BIND0), expected_ams_mapping=[0])
    assert any("are not project filament numbers" in i for i in r.issues), r.issues


def test_unparseable_slice_info() -> None:
    r = validate(_container("<config><plate>", _BIND0))
    assert any(i.startswith("G5 slice_info.config not parseable") for i in r.issues)


def test_mapping_longer_than_sixteen_is_rejected() -> None:
    r = validate(_container(_SINGLE_PETG, _BIND0), expected_ams_mapping=[0] * 17)
    assert any("at most 16" in i for i in r.issues), r.issues


# --------------------------------------------------------------------------- #
# G1–G4
# --------------------------------------------------------------------------- #


def test_not_a_zip() -> None:
    assert validate(b"not a zip").issues[0].startswith("G1 not a valid zip")


def test_duplicate_member_is_rejected() -> None:
    # Python reads the last copy; which one the printer runs is unknown.
    hot = b"M104 S350\n" + _BIND0
    with pytest.warns(UserWarning, match="Duplicate name"):
        data = _mk(
            [
                ("Metadata/plate_1.gcode", hot),
                ("Metadata/plate_1.gcode", _BIND0),
                ("Metadata/plate_1.gcode.md5", _md5(_BIND0)),
                ("Metadata/slice_info.config", _SINGLE_PETG.encode()),
            ]
        )
    assert validate(data).issues == ["G1 duplicate or encrypted archive members"]


def test_expanded_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validate_module, "_MAX_EXPANDED", 10)
    r = validate(_container(_SINGLE_PETG, _BIND0))
    assert r.issues == ["G1 archive exceeds the 4096-member / 512 MiB expanded limit"]


@pytest.mark.parametrize("missing", ["Metadata/plate_1.gcode", "Metadata/slice_info.config"])
def test_gate_required_members_are_the_three_it_reads(missing: str) -> None:
    members = {
        "Metadata/plate_1.gcode": _BIND0,
        "Metadata/plate_1.gcode.md5": _md5(_BIND0),
        "Metadata/slice_info.config": _SINGLE_PETG.encode(),
    }
    del members[missing]
    r = validate(_mk(members))
    assert any(i.startswith("G2") for i in r.issues), r.issues


@pytest.mark.parametrize(
    "md5",
    [
        hashlib.md5(_BIND0).hexdigest().encode(),  # noqa: S324 — lowercase
        _md5(_BIND0) + b"\n",
        _md5(b"other"),
    ],
)
def test_md5_contract(md5: bytes) -> None:
    data = _mk(
        {
            "Metadata/plate_1.gcode": _BIND0,
            "Metadata/plate_1.gcode.md5": md5,
            "Metadata/slice_info.config": _SINGLE_PETG.encode(),
        }
    )
    assert any(i.startswith("G3") for i in validate(data).issues)


@pytest.mark.parametrize(
    ("line", "issue"),
    [
        (b"M104 S300\n", "G4 nozzle 300"),
        (b"    M109 S290\n", "G4 nozzle 290"),
        (b"M190 S130\n", "G4 bed 130"),
    ],
)
def test_temperature_envelope(line: bytes, issue: str) -> None:
    r = validate(_container(_SINGLE_PETG, _BIND0 + line))
    assert any(i.startswith(issue) for i in r.issues), r.issues


# --------------------------------------------------------------------------- #
# G6 — Orca's is_same_printer_model / _is_same_nozzle_diameters
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("model", "header", "ok"),
    [
        ("C12", b"", True),
        ("C11", b"", True),  # Orca treats P1P and P1S slices as interchangeable
        ("", b"; printer_model = Bambu Lab P1S\n", True),  # Orca CLI output
        ("", b"; printer_model = Bambu Lab P1P\n", True),
        ("", b"", False),
        ("BL-P001", b"", False),  # X1C
        ("", b"; printer_model = Bambu Lab X1 Carbon\n", False),
    ],
)
def test_printer_model(model: str, header: bytes, ok: bool) -> None:
    r = validate(_container(_info(1, model=model), header + _BIND0), expected_nozzle=0.4)
    assert r.ok is ok, r.issues
    if not ok:
        assert any(i.startswith("G6 slice is for") for i in r.issues)


@pytest.mark.parametrize(
    ("meta", "header", "ok"),
    [
        ("0.4", b"", True),
        ("0.40", b"", True),
        ("", b"; nozzle_diameter = 0.4\n", True),
        ("0.6", b"", False),
        ("0.4,0.4", b"", False),  # dual-nozzle slice
        ("", b"", False),
    ],
)
def test_nozzle(meta: str, header: bytes, ok: bool) -> None:
    r = validate(_container(_info(1, nozzle=meta), header + _BIND0), expected_nozzle=0.4)
    assert r.ok is ok, r.issues
