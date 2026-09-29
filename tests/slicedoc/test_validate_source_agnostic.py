"""validate() must be source-agnostic — the print-#4 lesson, locked in.

The gate once required the donor's exact 15-member layout and compared gcode
S-numbers directly to ams_mapping values. That rejected the *hardware-proven*
OrcaSlicer slice (no thumbnails, no ``filament_maps``, gcode slice-index 0,
``ams_mapping=[1]``) while the donor's "printed air" file had passed. These
lock the corrected invariant: real-slicer shape passes, the §6.3 trap fails.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from bambu_bridge.slicedoc import validate


def _mk(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _container(slice_info: str, gcode: bytes) -> bytes:
    """Only the three members the gate reads — a real slicer's minimal shape."""
    return _mk(
        {
            "Metadata/plate_1.gcode": gcode,
            "Metadata/plate_1.gcode.md5": hashlib.md5(gcode)  # noqa: S324
            .hexdigest()
            .upper()
            .encode(),
            "Metadata/slice_info.config": slice_info.encode(),
        }
    )


_SINGLE_PETG = (
    '<?xml version="1.0"?><config><plate>'
    '<filament id="1" type="PETG" color="#161616"/>'
    "</plate></config>"
)


def test_real_slicer_shape_passes_no_thumbnails_no_filament_maps() -> None:
    # gcode in slice-filament-index space (S0); ams_mapping is the PHYSICAL
    # remap to tray 1 — its value must NOT be compared to the gcode index.
    gcode = b"M104 S250\nM140 S70\nM620 S0A\nT0\nG1 X1\nM621 S0A\n"
    r = validate(_container(_SINGLE_PETG, gcode), expected_ams_mapping=[1])
    assert r.ok, r.issues


def test_ams_mapping_value_is_not_constrained_to_gcode_index() -> None:
    # Slice index 0, physically loaded in tray 3 — perfectly valid.
    gcode = b"M620 S0A\nT0\nM621 S0A\n"
    r = validate(_container(_SINGLE_PETG, gcode), expected_ams_mapping=[3])
    assert r.ok, r.issues


def test_donor_synthesize_shape_passes_prebaked_physical_tray() -> None:
    # The donor/normalize path pre-bakes the physical tray (all selectors
    # rewritten to tray 1). Count == 1 filament -> still valid.
    gcode = b"M620 S1A\nT1\nM621 S1A\n"
    r = validate(_container(_SINGLE_PETG, gcode), expected_ams_mapping=[1])
    assert r.ok, r.issues


def test_6_3_split_handshake_still_rejected() -> None:
    # The 2026-05-19 recurrence: M620 S1A load disagrees with M621 S0A finish.
    gcode = b"M620 S1A\nT0\nG1 X1\nM621 S0A\n"
    r = validate(_container(_SINGLE_PETG, gcode), expected_ams_mapping=[1])
    assert not r.ok
    joined = " ".join(r.issues)
    assert "handshake incoherent" in joined  # (a) catches the split
    assert "distinct tray" in joined  # (b) catches the arity too


def test_arity_mismatch_between_ams_mapping_and_filaments_rejected() -> None:
    gcode = b"M620 S0A\nT0\nM621 S0A\n"
    r = validate(_container(_SINGLE_PETG, gcode), expected_ams_mapping=[1, 2])
    assert not r.ok
    assert any("ams_mapping arity" in i for i in r.issues)


def test_filament_maps_mismatch_still_caught_when_present() -> None:
    # Bambu-Studio donor shape: a filament_maps token list that disagrees
    # with the <filament> count is the original §6.3 trap.
    slice_info = (
        '<?xml version="1.0"?><config><plate>'
        '<metadata key="filament_maps" value="1 2 3"/>'
        '<filament id="1" type="PETG" color="#161616"/>'
        "</plate></config>"
    )
    gcode = b"M620 S0A\nT0\nM621 S0A\n"
    r = validate(_container(slice_info, gcode), expected_ams_mapping=[1])
    assert not r.ok
    assert any("filament arity mismatch" in i for i in r.issues)


@pytest.mark.parametrize("missing", ["Metadata/plate_1.gcode", "Metadata/slice_info.config"])
def test_gate_required_members_are_the_three_it_reads(missing: str) -> None:
    members = {
        "Metadata/plate_1.gcode": b"M620 S0A\nM621 S0A\n",
        "Metadata/plate_1.gcode.md5": hashlib.md5(  # noqa: S324
            b"M620 S0A\nM621 S0A\n"
        )
        .hexdigest()
        .upper()
        .encode(),
        "Metadata/slice_info.config": _SINGLE_PETG.encode(),
    }
    del members[missing]
    r = validate(_mk(members))
    assert not r.ok
    assert any(i.startswith("G2") for i in r.issues)


@pytest.mark.parametrize(
    ("model", "nozzle", "issue"),
    [
        ("C12", "0.4", None),
        ("C11", "0.4", "requires P1S"),
        ("C12", "0.6", "differs from the reported nozzle"),
        ("C12", "0.4,0.4", "single-nozzle"),
        ("", "", "requires P1S"),
    ],
)
def test_target_model_and_nozzle_qualification(model, nozzle, issue) -> None:
    metadata = (
        f'<metadata key="printer_model_id" value="{model}"/>'
        f'<metadata key="nozzle_diameters" value="{nozzle}"/>'
    )
    xml = _SINGLE_PETG.replace("<plate>", "<plate>" + metadata)
    result = validate(
        _container(xml, b"M620 S0A\nT0\nM621 S0A\n"),
        expected_model="C12",
        expected_nozzle=0.4,
        expected_ams_mapping=[0],
    )
    if issue is None:
        assert result.ok, result.issues
    else:
        assert not result.ok
        assert any(issue in item for item in result.issues)


# A two-colour flush inlay sliced by OrcaSlicer 2.4.2 with per-part filaments: the layers that print
# both colours list both slice indices, space-separated (captured from a real sliced plate).
_TWO_FILAMENT_INLAY = (
    '<?xml version="1.0"?><config><plate>'
    '<metadata key="filament_maps" value="1 1"/>'
    '<filament id="1" type="ASA" color="#161616"/>'
    '<filament id="2" type="ASA" color="#FFF144"/>'
    "<layer_filament_lists>"
    '<layer_filament_list filament_list="0 1" layer_ranges="8 9" />'
    '<layer_filament_list filament_list="0" layer_ranges="0 7" />'
    "</layer_filament_lists>"
    "</plate></config>"
)
_TWO_FILAMENT_GCODE = (
    b"M620 S0A\nT0\nM621 S0A\nG1 X1\n" b"M620 S1A\nT1\nM621 S1A\nM620 S0A\nT0\nM621 S0A\n"
)


def test_multi_filament_layer_list_is_parsed_not_a_crash() -> None:
    # Regression: int("0 1") raised ValueError out of validate(), so every flush two-colour
    # slice failed the jobs API, the Orca upload adapter and the queue with a server error.
    r = validate(_container(_TWO_FILAMENT_INLAY, _TWO_FILAMENT_GCODE), expected_ams_mapping=[3, 1])
    assert r.ok, r.issues


@pytest.mark.parametrize("value", ["0 x", "one", "0,1", "1_0", "0 \u0661"])
def test_unparseable_layer_list_is_an_issue_not_an_exception(value: str) -> None:
    info = _TWO_FILAMENT_INLAY.replace('filament_list="0 1"', f'filament_list="{value}"')
    r = validate(_container(info, _TWO_FILAMENT_GCODE), expected_ams_mapping=[3, 1])
    assert not r.ok
    assert any("is not a list of indices" in issue for issue in r.issues), r.issues


def test_layer_list_index_outside_the_filaments_still_rejected() -> None:
    info = _TWO_FILAMENT_INLAY.replace('filament_list="0 1"', 'filament_list="0 2"')
    r = validate(_container(info, _TWO_FILAMENT_GCODE), expected_ams_mapping=[3, 1])
    assert any("filament_list=2 outside 0..1" in issue for issue in r.issues), r.issues


@pytest.mark.parametrize("attr", ['filament_list=""', ""])
def test_missing_or_empty_layer_list_counts_as_out_of_range(attr: str) -> None:
    info = _TWO_FILAMENT_INLAY.replace('filament_list="0 1"', attr)
    r = validate(_container(info, _TWO_FILAMENT_GCODE), expected_ams_mapping=[3, 1])
    assert not r.ok
    assert any("filament_list=-1 outside 0..1" in issue for issue in r.issues), r.issues
