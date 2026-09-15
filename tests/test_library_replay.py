from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest

from bambu_bridge.library import Artifact, Capture, LibraryError, LibraryStore
from bambu_bridge.library_replay import requirements, review
from bambu_bridge.service.material_inventory import MaterialInventory, inventory_view


def sliced(*, indices=(0, 1), plate=1, model="C12", gcode=None, xml=None) -> bytes:
    if gcode is None:
        gcode = b"M104 S220\nM140 S60\n" + b"".join(
            f"M620 S{i}A\nT{i}\nM621 S{i}A\n".encode() for i in indices
        )
    if xml is None:
        xml = (
            f'<config><plate><metadata key="index" value="{plate}"/>'
            f'<metadata key="printer_model_id" value="{model}"/>'
            '<metadata key="nozzle_diameters" value="0.4"/>'
            + "".join(
                f'<filament id="{i+1}" type="PLA" color="00FF00FF" '
                'tray_info_idx="GFA00" used_g="10"/>'
                for i in indices
            )
            + "</plate></config>"
        )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"Metadata/plate_{plate}.gcode", gcode)
        archive.writestr(
            f"Metadata/plate_{plate}.gcode.md5", hashlib.md5(gcode).hexdigest().upper()
        )
        archive.writestr("Metadata/slice_info.config", xml)
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps(
                {
                    "curr_bed_type": "Textured PEI Plate",
                    "filament_settings_id": ["Generic PLA"] * 4,
                }
            ),
        )
    return stream.getvalue()


def materials(*, unit_ids=(0,), reverse=False) -> dict:
    units = [
        {
            "id": str(aid),
            "tray": [
                {
                    "id": str(i),
                    "tray_type": "PLA",
                    "tray_color": "00FF00FF",
                    "tray_info_idx": "GFA00",
                    "tray_uuid": f"spool-{aid}-{i}",
                }
                for i in (reversed(range(4)) if reverse else range(4))
            ],
        }
        for aid in unit_ids
    ]
    return {
        "ams": {
            "ams": units,
            "ams_exist_bits": format(sum(1 << i for i in unit_ids), "x"),
            "tray_exist_bits": format(sum(15 << (i * 4) for i in unit_ids), "x"),
        }
    }


def frame(**kwargs) -> dict:
    tracker = MaterialInventory()
    tracker.observe(materials(**kwargs))
    return tracker.snapshot()


def archive_slice(store: LibraryStore, data: bytes, *, plate: int = 1) -> str:
    capture = Capture(
        id="d" * 32,
        title="Replay fixture",
        slicer_version="fixture",
        plate=plate,
        artifacts=(
            Artifact(
                name="slice.3mf",
                role="slice",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            ),
        ),
    )
    store.create("fixture", capture)
    store.append(capture.id, "slice.3mf", 0, data, "fixture")
    store.finalize(capture.id, "fixture")
    return capture.id


PRINTER = {"printer_id": "FIXTURE", "connected": True, "model": "P1S"}


def test_moved_spools_use_logical_indices_and_leave_original_slice_untouched(tmp_path: Path):
    store = LibraryStore(tmp_path)
    data = sliced(indices=(0, 2), plate=2)  # unused logical filament 1 needs no choice
    cid = archive_slice(store, data, plate=2)
    inventory = frame(reverse=True)
    result = review(store, cid, printer=PRINTER, frame=inventory, choices={0: 3, 2: 1})
    assert result["mapping_complete"] is True, result["issues"]
    assert [(f["index"], f["choice"]) for f in result["mapping"]] == [(0, 3), (2, 1)]
    assert result["requirements"]["plate"] == 2
    assert result["dispatch_available"] is False
    assert store.download(cid, "slice.3mf")[0].read_bytes() == data
    assert store.get(cid)["attempts"] == []  # analysis must never invent a print


def test_standard_multi_ams_ids_are_not_array_positions():
    view = inventory_view(frame(unit_ids=(2, 0), reverse=True))
    assert view["issues"] == []
    assert [s["wire_id"] for s in view["slots"]] == [0, 1, 2, 3, 8, 9, 10, 11]
    assert view["slots"][4]["label"] == "AMS C · slot 1"
    assert all("spool-" not in json.dumps(s) for s in view["slots"])


def test_temperature_updates_cannot_refresh_inventory_or_hide_partial_change(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("bambu_bridge.service.material_inventory.time.time", lambda: clock[0])
    tracker = MaterialInventory()
    tracker.observe(materials())
    assert inventory_view(tracker.snapshot())["ams_fresh"]
    clock[0] = 131
    tracker.observe({"nozzle_temper": 220})
    tracker.observe({"ams": {"humidity_raw": 18}})
    assert not inventory_view(tracker.snapshot())["ams_fresh"]
    tracker.observe(materials())
    tracker.observe({"ams": {"tray_exist_bits": "7"}})
    assert inventory_view(tracker.snapshot())["slots"] == []
    tracker.observe(materials())
    tracker.clear()  # reconnect must seed a new inventory
    assert inventory_view(tracker.snapshot())["slots"] == []


def test_changed_inventory_wrong_material_missing_and_duplicate_trays_block_review(tmp_path):
    store = LibraryStore(tmp_path)
    cid = archive_slice(store, sliced())
    current = frame()
    before = inventory_view(current)["fingerprint"]
    current["ams"]["ams"][0]["tray"][3]["tray_uuid"] = "different-spool-same-color"
    result = review(
        store, cid, printer=PRINTER, frame=current, choices={0: 3, 1: 1}, expected_inventory=before
    )
    assert not result["mapping_complete"] and any("changed" in s for s in result["issues"])
    current["ams"]["ams"][0]["tray"][3]["tray_type"] = "ABS"
    result = review(store, cid, printer=PRINTER, frame=current, choices={0: 3, 1: 1})
    assert any("different material" in s for s in result["issues"])
    current["ams"]["tray_exist_bits"] = "7"
    assert not inventory_view(current)["slots"][3]["present"]
    current["ams"]["ams"][0]["tray"][3]["id"] = "2"
    assert inventory_view(current)["slots"] == []


def test_external_spool_needs_fresh_report_and_single_used_filament(tmp_path):
    store = LibraryStore(tmp_path)
    cid = archive_slice(store, sliced(indices=(0,)))
    tracker = MaterialInventory()
    tracker.observe({"vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "00FF00FF"}})
    external = tracker.snapshot()
    result = review(store, cid, printer=PRINTER, frame=external, choices={0: 254})
    assert result["mapping_complete"], result["issues"]
    external["external_at"] = time.time() - 31
    assert not review(store, cid, printer=PRINTER, frame=external, choices={0: 254})[
        "mapping_complete"
    ]
    other = LibraryStore(tmp_path / "multi")
    other_id = archive_slice(other, sliced())
    result = review(
        other, other_id, printer=PRINTER, frame=tracker.snapshot(), choices={0: 254, 1: 254}
    )
    assert any("exactly one" in s for s in result["issues"])


def test_prebaked_physical_tray_is_not_treated_as_a_remappable_logical_tool():
    data = sliced(indices=(0,), gcode=b"M104 S220\nM140 S60\nM620 S3A\nT3\nM621 S3A\n")
    assert any("ambiguous" in issue for issue in requirements(data, 1)["issues"])


def test_observed_orca_external_slice_with_load_finish_and_no_explicit_tool():
    # Observed on the user's printing Orca 2.4.2 slice (exact bytes checked
    # privately): the mapping handshake exists without an executable T0.
    data = sliced(
        indices=(0,),
        gcode=(
            b"M104 S250\nM140 S55\nM620 M\nM620 S0A\nM621 S0A\nT1000\nM620 S255\nT255\nM621 S255\n"
        ),
    )
    spec = requirements(data, 1)
    assert spec["issues"] == []
    assert spec["filaments"][0]["used"]


def test_unknown_model_changed_certificate_and_different_nozzle_require_review(tmp_path):
    store = LibraryStore(tmp_path)
    cid = archive_slice(store, sliced(indices=(0,)))
    for changes, expected in [
        ({"model": None}, "not recorded"),
        ({"cert_status": "changed"}, "certificate changed"),
        ({"nozzle_diameter": "0.6"}, "diameter differs"),
        ({"connected": False}, "disconnected"),
    ]:
        result = review(store, cid, printer={**PRINTER, **changes}, frame=frame(), choices={0: 0})
        assert not result["mapping_complete"]
        assert any(expected in message for message in result["issues"])


@pytest.mark.parametrize(
    "xml",
    [
        '<!DOCTYPE config [<!ENTITY bad "expanded">]><config>&bad;</config>',
        '<config><plate><metadata key="index" value="1"/>'
        '<metadata key="index" value="2"/></plate></config>',
        '<config><plate><metadata key="index" value="2"/></plate></config>',
    ],
)
def test_xml_entities_or_ambiguous_plates_are_rejected(xml):
    with pytest.raises(LibraryError):
        requirements(sliced(xml=xml), 1)


def test_corrupt_checksum_duplicate_members_and_expansion_limits_are_rejected(monkeypatch):
    data = sliced()
    stream = io.BytesIO(data)
    with pytest.warns(UserWarning), zipfile.ZipFile(stream, "a") as archive:
        archive.writestr("Metadata/plate_1.gcode.md5", "A" * 32)
    with pytest.raises(LibraryError):
        requirements(stream.getvalue(), 1)
    monkeypatch.setattr("bambu_bridge.library_replay._EXPANDED_LIMIT", 50)
    with pytest.raises(LibraryError):
        requirements(data, 1)
