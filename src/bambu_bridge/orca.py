"""Revocable, printer-scoped slicer credentials; never accepted by the main API."""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from bambu_bridge.pairing import PairingStore, digest


class OrcaStore:
    def __init__(self, pairing: PairingStore):
        self.pairing = pairing
        with pairing.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS slicers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, printer_id TEXT NOT NULL,
                hash TEXT UNIQUE NOT NULL, mapping TEXT, created INTEGER NOT NULL,
                revoked INTEGER)""")

    def create(self, name: str, printer_id: str, mapping: list[int] | None) -> dict[str, Any]:
        token = "bbs_" + secrets.token_urlsafe(32)
        client_id = secrets.token_hex(12)
        created = int(time.time())
        with self.pairing.connect() as db:
            db.execute(
                "INSERT INTO slicers VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    client_id,
                    name,
                    printer_id,
                    digest(token),
                    json.dumps(mapping) if mapping is not None else None,
                    created,
                ),
            )
        return {
            "id": client_id,
            "name": name,
            "printer_id": printer_id,
            "token": token,
            "ams_mapping": mapping,
            "created": created,
        }

    def authenticate(self, token: str | None, printer_id: str) -> dict[str, Any] | None:
        if not token or not token.startswith("bbs_") or len(token) > 128:
            return None
        with self.pairing.connect() as db:
            row = db.execute(
                "SELECT id, mapping FROM slicers WHERE hash = ? AND printer_id = ? "
                "AND revoked IS NULL",
                (digest(token), printer_id),
            ).fetchone()
        return (
            {
                "id": row["id"],
                "ams_mapping": json.loads(row["mapping"]) if row["mapping"] is not None else None,
            }
            if row
            else None
        )

    def clients(self) -> list[dict[str, Any]]:
        with self.pairing.connect() as db:
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "printer_id": row["printer_id"],
                    "ams_mapping": json.loads(row["mapping"])
                    if row["mapping"] is not None
                    else None,
                    "created": row["created"],
                    "revoked": row["revoked"],
                }
                for row in db.execute("SELECT * FROM slicers ORDER BY created")
            ]

    def revoke(self, client_id: str) -> None:
        with self.pairing.connect() as db:
            db.execute(
                "UPDATE slicers SET revoked = ? WHERE id = ? AND revoked IS NULL",
                (int(time.time()), client_id),
            )
