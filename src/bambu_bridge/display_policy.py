"""Provider-namespaced native display preferences; qualification is not a preference."""

from __future__ import annotations

import json
from typing import Any

from bambu_bridge.inventory_journal import InventoryConflict, InventoryJournal


class DisplayPolicy:
    def __init__(self, journal: InventoryJournal):
        self.journal = journal

    async def snapshot(self, provider_id: str) -> dict[str, Any]:
        async with self.journal._lock:
            return await self._snapshot(provider_id)

    async def _snapshot(self, provider_id: str) -> dict[str, Any]:
        async with self.journal._conn.execute(
            "SELECT * FROM inventory_display_policy WHERE provider_id=?", (provider_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return {
            "provider_id": provider_id,
            "revision": row["revision"] if row else 0,
            "global_enabled": bool(row["enabled"]) if row else False,
            "printers": json.loads(row["printers"]) if row else [],
        }

    async def replace(
        self, provider_id: str, revision: int, enabled: bool, printers: list[str]
    ) -> dict[str, Any]:
        if type(enabled) is not bool or type(revision) is not int or revision < 0:
            raise ValueError("a boolean preference and nonnegative revision are required")
        if (
            len(printers) > 64
            or len(set(printers)) != len(printers)
            or any(
                not isinstance(p, str) or not p or len(p) > 128 or not p.isprintable()
                for p in printers
            )
        ):
            raise ValueError("printer identities must be distinct nonempty strings")
        async with self.journal._lock:
            try:
                await self.journal._conn.execute("BEGIN IMMEDIATE")
                current = await self._snapshot(provider_id)
                if current["revision"] != revision:
                    raise InventoryConflict("display preferences changed; reload before editing")
                await self.journal._conn.execute(
                    "INSERT INTO inventory_display_policy VALUES (?,?,?,?) "
                    "ON CONFLICT(provider_id) DO UPDATE SET revision=excluded.revision,"
                    "enabled=excluded.enabled,printers=excluded.printers",
                    (provider_id, revision + 1, int(enabled), json.dumps(sorted(printers))),
                )
                result = await self._snapshot(provider_id)
                await self.journal._conn.commit()
                return result
            except BaseException:
                await self.journal._conn.rollback()
                raise
