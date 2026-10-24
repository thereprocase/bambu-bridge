import hashlib
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bambu_bridge.native_gateway import NativeGateway
from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.service.shadow_accounting import reserve_shadow
from bambu_bridge.spool_assignments import AssignmentSnapshot, SpoolAssignment
from bambu_bridge.spoolman import ProviderSpool


def archive(entries):
    """A .gcode.3mf-shaped zip with the given (member name, text) entries."""
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as bundle:
        for name, content in entries:
            bundle.writestr(name, content)
    result.seek(0)
    return result


def fixture(tmp_path, monkeypatch):
    inbox = NativeInbox(tmp_path)
    row = inbox.reserve("fixture", "/part.3mf", 100000)
    identifier = row["id"]
    content = archive(
        [
            (
                "Metadata/plate_1.gcode",
                "; filament used [g] = 2.0, 6.0\nM83\nM73 L1\nT0\nG1 E10\n"
                "M73 L2\nT1\nG1 E30\nT0\nG1 E10",
            )
        ]
    ).getvalue()
    inbox.payload(identifier).write_bytes(content)
    command = {
        "print": {
            "command": "project_file",
            "url": "file:///sdcard/part.3mf",
            "param": "Metadata/plate_1.gcode",
            "ams_mapping": [0, 3],
            "use_ams": True,
        }
    }
    with inbox.connect() as db:
        db.execute(
            "UPDATE uploads SET state='delivered',start_state='queued',sha256=?,"
            "bytes=?,command=? WHERE id=?",
            (hashlib.sha256(content).hexdigest(), len(content), json.dumps(command), identifier),
        )
    assignment = SpoolAssignment("fixture", "ams:hardware:tray:0", 7, observed_identity="identity")
    snapshot = AssignmentSnapshot("provider", 12, (assignment,))
    choices = [
        {
            "source": assignment.source,
            "wire_slot": 0,
            "assignable": True,
            "observed_identity": "identity",
            "material": "PETG",
        }
    ]
    spool = ProviderSpool.model_validate(
        {
            "id": 7,
            "remaining_weight": 100,
            "filament": {
                "id": 2,
                "material": "PETG",
                "diameter": 1.75,
                "density": 1.28,
                "weight": 1000,
            },
        }
    )
    inventory = SimpleNamespace(
        assignments=SimpleNamespace(snapshot=AsyncMock(return_value=snapshot)),
        identity=SimpleNamespace(verify=AsyncMock()),
        provider=SimpleNamespace(get_spool=AsyncMock(return_value=spool)),
    )
    return inbox, identifier, inventory, snapshot, choices


async def test_dispatch_freezes_layer_grams_slots_and_spools(tmp_path, monkeypatch):
    inbox, identifier, inventory, snapshot, _ = fixture(tmp_path, monkeypatch)
    external = SpoolAssignment("fixture", "external", 9, observed_identity="identity")
    inventory.assignments.snapshot.return_value = AssignmentSnapshot(
        "provider",
        12,
        (
            snapshot.assignments[0].__class__(
                "fixture", "ams:hardware:tray:0", 7, observed_identity="identity", wire_slot=0
            ),
            external,
        ),
    )
    await reserve_shadow(inbox, identifier, inventory, "fixture", object())
    with inbox.connect() as db:
        frozen = json.loads(db.execute("SELECT manifest FROM accounting_manifests").fetchone()[0])
    assert frozen["layer_grams"] == [{"0": 1.0}, {"0": 1.0, "1": 6.0}]
    assert frozen["tool_wires"] == {"0": 0, "1": 3}  # slot 4 has no spool: noted at the end
    assert frozen["wire_spools"] == {"0": 7, "254": 9}
    assert frozen["planned_grams"] == 8.0
    assert frozen["artifact_sha256"] == inbox.get(identifier)["sha256"]


async def test_external_prints_map_every_filament_to_the_holder(tmp_path, monkeypatch):
    inbox, identifier, inventory, _, _ = fixture(tmp_path, monkeypatch)
    with inbox.connect() as db:
        command = json.loads(db.execute("SELECT command FROM uploads").fetchone()[0])
        command["print"].update(use_ams=False, ams_mapping=[-1, -1])
        db.execute("UPDATE uploads SET command=?", (json.dumps(command),))
    await reserve_shadow(inbox, identifier, inventory, "fixture", object())
    with inbox.connect() as db:
        frozen = json.loads(db.execute("SELECT manifest FROM accounting_manifests").fetchone()[0])
    assert frozen["tool_wires"] == {"0": 254, "1": 254}


async def test_an_unreadable_archive_is_noted_and_never_frozen(tmp_path, monkeypatch):
    inbox, identifier, inventory, _, _ = fixture(tmp_path, monkeypatch)
    inbox.payload(identifier).write_bytes(b"not a zip")
    gateway = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(inventory=inventory)),
        inbox=inbox,
        config={"printer_id": "fixture"},
        service=lambda: object(),
    )
    await NativeGateway.prepare_shadow_accounting(gateway, identifier)
    with inbox.connect() as db:
        review = db.execute("SELECT * FROM accounting_reviews").fetchone()
    assert review["reason"] == "analysis_or_source_unverified"
    assert inbox.accounting_events() == []
