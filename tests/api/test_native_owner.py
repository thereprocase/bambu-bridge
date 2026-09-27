"""Native gateway administration accepts owner keys and paired phones over HTTPS."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from bambu_bridge.config import Settings
from bambu_bridge.main import create_app
from bambu_bridge.native_inbox import NativeInbox


def test_native_owner_boundary_and_no_store(tmp_path):
    app = create_app(
        Settings(
            bridge_api_key="native-fixture-owner",
            bridge_db_path=str(tmp_path / "jobs.db"),
            bridge_pairing_dir=str(tmp_path / "pairing"),
        )
    )
    owner = {"Authorization": "Bearer native-fixture-owner"}
    with TestClient(app, base_url="https://bridge.invalid") as client:
        gateway = SimpleNamespace(
            status=lambda: {"configured": True, "enabled": False},
            saved_code=lambda: "FIXTURE1",
            authenticate=AsyncMock(return_value=True),
            enable=AsyncMock(return_value={"access_code": "FIXTURE1", "enabled": True}),
            disable=AsyncMock(),
            announce=AsyncMock(),
            setup_status=lambda _: {"printer_connected": False, "camera_streaming": False},
            close=AsyncMock(),
            inbox_dispatch_lock=asyncio.Lock(),
            ensure_idle=AsyncMock(),
            service=lambda: SimpleNamespace(
                native_snapshot=lambda: {"print": {"gcode_state": "FINISH"}}
            ),
        )
        app.state.native_gateway = gateway
        app.state.registry.get = lambda _: SimpleNamespace(model="P1S")
        for method, path in [
            ("GET", "/native"),
            ("GET", "/native/access-code"),
            ("GET", "/native/setup"),
            ("GET", "/native/setup-status"),
            ("POST", "/native/access-code"),
            ("POST", "/native"),
            ("DELETE", "/native"),
            ("POST", "/native/announce"),
            ("POST", "/native/readiness"),
            ("POST", "/native/uploads/" + "a" * 32),
        ]:
            kwargs = {"json": {"printer_id": "FIXTURE"}} if method == "POST" else {}
            assert client.request(method, "/api/v1" + path, **kwargs).status_code == 401
            assert (
                client.request(
                    method, "http://bridge.invalid/api/v1" + path, headers=owner, **kwargs
                ).status_code
                == 403
            )
        response = client.post("/api/v1/native", headers=owner, json={"printer_id": "FIXTURE"})
        ready = client.post("/api/v1/native/readiness", headers=owner)
        assert ready.json() == {"ready": True, "gcode_state": "FINISH", "print_commands_sent": 0}
        gateway.ensure_idle.assert_awaited_once_with(force_refresh=True)
        gateway.ensure_idle.side_effect = ValueError("BBSTART_NOT_IDLE")
        assert client.post("/api/v1/native/readiness", headers=owner).status_code == 409
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "access_code" not in client.get("/api/v1/native", headers=owner).json()
        with patch("bambu_bridge.api.native.setup_material", return_value={"script": "fixture"}):
            setup = client.get("/api/v1/native/setup", headers=owner)
            assert setup.status_code == 200 and setup.json() == {"script": "fixture"}
            assert setup.headers["cache-control"] == "no-store"
            assert setup.headers["referrer-policy"] == "no-referrer"
        check = client.get("/api/v1/native/setup-status", headers=owner)
        assert check.json() == {"printer_connected": False, "camera_streaming": False}
        assert check.headers["cache-control"] == "no-store"
        saved = client.post(
            "/api/v1/native/access-code", headers=owner, json={"access_code": "FIXTURE1"}
        )
        assert saved.json() == {"access_code": "FIXTURE1"}
        assert saved.headers["cache-control"] == "no-store"
        gateway.authenticate.return_value = False
        rejected = client.post(
            "/api/v1/native/access-code", headers=owner, json={"access_code": "WRONG123"}
        )
        assert rejected.status_code == 422 and "WRONG123" not in rejected.text
        for _ in range(2):
            saved = client.get("/api/v1/native/access-code", headers=owner)
            assert saved.json() == {"access_code": "FIXTURE1"}
            assert saved.headers["cache-control"] == "no-store"
        invitation, _ = app.state.pairing.invite()
        phone = app.state.pairing.claim(invitation, "Fixture phone")
        assert phone is not None
        paired = {"Authorization": "Bearer " + phone["token"]}
        assert client.get("/api/v1/native/access-code", headers=paired).json() == {
            "access_code": "FIXTURE1"
        }
        assert client.get("/api/v1/native/setup-status", headers=paired).status_code == 200
        assert client.get("http://bridge.invalid/api/v1/native", headers=paired).status_code == 401
        assert client.delete("/api/v1/native", headers=owner).status_code == 204
        gateway.disable.assert_awaited_once()
        app.state.registry.get = lambda _: SimpleNamespace(model="A1")
        assert (
            client.post("/api/v1/native", headers=owner, json={"printer_id": "FIXTURE"}).status_code
            == 422
        )
        inbox = NativeInbox(tmp_path / "recovery")
        identifier = inbox.claim_external(
            "FIXTURE",
            {
                "print": {
                    "command": "project_file",
                    "sequence_id": "fixture-1",
                    "url": "file:///sdcard/test.3mf",
                }
            },
        )
        inbox.dispatched(identifier, "unknown")
        gateway.inbox = inbox
        gateway.config = {"printer_id": "FIXTURE"}
        gateway.store = SimpleNamespace(directory=tmp_path / "recovery")
        gateway.change_lock = asyncio.Lock()
        gateway.inbox_dispatch_lock = asyncio.Lock()
        gateway.inbox_wake = asyncio.Event()
        gateway.require_idle = Mock(side_effect=ValueError("BBSTART_NOT_IDLE"))
        action = {"action": "resolve", "confirm": "I checked the printer and this action"}
        path = "/api/v1/native/uploads/" + identifier
        assert client.post(path, headers=owner, json=action).status_code == 409
        assert inbox.get(identifier)["start_state"] == "unknown"
        gateway.require_idle.side_effect = None
        assert client.post(path, headers=owner, json={"action": "resolve"}).status_code == 422
        assert client.post(path, headers=owner, json=action).status_code == 200
        assert inbox.get(identifier)["start_state"] == "resolved"
        second = inbox.claim_external(
            "FIXTURE", {"print": {"command": "project_file", "url": "file:///sdcard/phone.3mf"}}
        )
        inbox.dispatched(second, "unknown")
        phone_action = client.post("/api/v1/native/uploads/" + second, headers=paired, json=action)
        assert phone_action.status_code == 200
        assert inbox.get(second)["start_state"] == "resolved"
        inbox.claim_external(
            "OLDER_PRINTER",
            {"print": {"command": "project_file", "url": "file:///sdcard/old.3mf"}},
        )
        queue = client.get("/api/v1/native/queue", headers=paired)
        assert queue.status_code == 200
        assert queue.json()["start_owner"] is None
        assert {row["id"] for row in queue.json()["uploads"]} == {identifier, second}
        first_page = client.get("/api/v1/native/queue?limit=1", headers=paired).json()
        second_page = client.get("/api/v1/native/queue?limit=1&offset=1", headers=paired).json()
        assert first_page["has_more"] and not second_page["has_more"]
        assert first_page["uploads"][0]["id"] != second_page["uploads"][0]["id"]
        with inbox.connect() as db:
            db.execute(
                "INSERT INTO uploads (id,printer,logical,remote,state,start_state,code,created) "
                "VALUES (?,'FIXTURE','/failed.3mf','/failed.3mf','failed','blocked',"
                "'BBDELIVERY_FAILED',unixepoch())",
                ("f" * 32,),
            )
        review = client.get("/api/v1/native/queue?view=review", headers=paired)
        assert review.status_code == 200
        assert review.json()["review_count"] == 1
        assert review.json()["acknowledgeable_count"] == 1
        assert [row["id"] for row in review.json()["uploads"]] == ["f" * 32]
        assert client.get("/api/v1/native/queue?view=invalid", headers=paired).status_code == 422
        assert client.post("/api/v1/native/queue/acknowledge", headers=paired).status_code == 422
        gateway.require_idle.side_effect = ValueError("BBSTART_NOT_IDLE")
        acknowledged = client.post(
            "/api/v1/native/queue/acknowledge", headers=paired,
            json={"confirm": "Ignore past review warnings; keep active starts"},
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json()["acknowledged"] == 1
        assert acknowledged.json()["review_count"] == 0
        assert inbox.get("f" * 32)["state"] == "failed"
        assert inbox.get("f" * 32)["code"] == "BBDELIVERY_FAILED"
        remaining_review = client.get("/api/v1/native/queue?view=review", headers=paired)
        assert remaining_review.json()["uploads"] == []
        backup = client.post("/api/v1/native/recovery/backups", headers=paired)
        assert backup.status_code == 201
        backup_id = backup.json()["id"]
        listed = client.get("/api/v1/native/recovery/backups", headers=paired).json()
        assert listed[0]["id"] == backup_id
        exported = client.get(f"/api/v1/native/recovery/backups/{backup_id}", headers=paired)
        assert exported.status_code == 200 and exported.content[:2] == b"PK"
        imported = client.post(
            "/api/v1/native/recovery/backups/import",
            headers=paired,
            files={"file": ("native.zip", exported.content, "application/zip")},
        )
        assert imported.status_code == 201
        invalid = client.post(
            "/api/v1/native/recovery/backups/import",
            headers=paired,
            files={"file": ("broken.zip", b"not a backup", "application/zip")},
        )
        assert invalid.status_code == 409
        imported_id = imported.json()["id"]
        assert client.delete(
            f"/api/v1/native/recovery/backups/{imported_id}", headers=paired
        ).status_code == 204
        remaining = client.get("/api/v1/native/recovery/backups", headers=paired).json()
        assert imported_id not in {row["id"] for row in remaining}
