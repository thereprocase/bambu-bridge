"""Durable intent log for non-idempotent Spoolman writes.

An intent is committed before network I/O. Once delivery starts, an unknown
outcome blocks that resource until explicit reconciliation. A process crash
never turns an in-flight write into an automatic retry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite


class InventoryConflict(Exception):
    """An operation token was reused or a spool needs reconciliation."""


class InventoryJournal:
    """The bridge's own inventory tables: stock-material selections and display policy."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> InventoryJournal:
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS inventory_material_revision (
                    provider_id TEXT PRIMARY KEY, revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inventory_material_mappings (
                    provider_id TEXT NOT NULL, product_id INTEGER NOT NULL,
                    stock_key TEXT NOT NULL, catalogue_digest TEXT NOT NULL,
                    product_fingerprint TEXT NOT NULL, PRIMARY KEY(provider_id,product_id)
                );
                CREATE TABLE IF NOT EXISTS inventory_display_policy (
                    provider_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), printers TEXT NOT NULL
                );
            """)
            await conn.commit()
        except BaseException:
            await conn.close()
            raise
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()
