"""Stock-Orca material identities and the per-provider product selections.

The catalogue is the pinned list of stock Orca profiles shipped with the bridge
(no preset or configuration file is written). A selection ties one provider
product to one catalogue entry and is revisioned per provider.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any

from bambu_bridge.inventory_journal import InventoryConflict, InventoryJournal
from bambu_bridge.spoolman import Product


@dataclass(frozen=True)
class StockMaterial:
    key: str
    name: str
    material_id: str
    material: str
    resource_sha256: str
    inheritance_sha256: str


@lru_cache(maxsize=1)
def _load() -> tuple[str, tuple[StockMaterial, ...]]:
    content = files("bambu_bridge").joinpath("data/stock_materials_v242.json").read_bytes()
    data = json.loads(content)
    profiles = tuple(StockMaterial(**record) for record in data["profiles"])
    if len({p.key for p in profiles}) != len(profiles):
        raise ValueError("duplicate stock profile keys")
    return hashlib.sha256(content).hexdigest(), profiles


def catalogue() -> tuple[StockMaterial, ...]:
    return _load()[1]


def catalogue_digest() -> str:
    return _load()[0]


def product_fingerprint(product: Product) -> str:
    # Color/quantity corrections do not change the chosen stock material family.
    identity = {
        "id": product.id,
        "name": product.name,
        "material": product.material,
        "diameter": product.diameter,
        "vendor": {"id": product.vendor.id, "name": product.vendor.name}
        if product.vendor
        else None,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class MaterialMappings:
    def __init__(self, journal: InventoryJournal):
        self.journal = journal
        self.conn = journal._conn

    async def snapshot(self, provider_id: str) -> dict[str, Any]:
        async with self.journal._lock:
            return await self._snapshot(provider_id)

    async def _snapshot(self, provider_id: str) -> dict[str, Any]:
        async with self.conn.execute(
            "SELECT revision FROM inventory_material_revision WHERE provider_id=?", (provider_id,)
        ) as cursor:
            row = await cursor.fetchone()
        async with self.conn.execute(
            "SELECT product_id,stock_key,catalogue_digest,product_fingerprint "
            "FROM inventory_material_mappings WHERE provider_id=? ORDER BY product_id",
            (provider_id,),
        ) as cursor:
            mappings = [dict(row) for row in await cursor.fetchall()]
        return {"provider_id": provider_id, "revision": row[0] if row else 0, "mappings": mappings}

    async def select(
        self, provider_id: str, product: Product, stock_key: str | None, *, revision: int
    ) -> dict[str, Any]:
        selected = next((p for p in catalogue() if p.key == stock_key), None)
        if stock_key is not None and selected is None:
            raise ValueError("select a pinned stock material from the catalogue")
        if selected is not None and (
            not product.material or selected.material.upper() != product.material.strip().upper()
        ):
            raise ValueError("stock material chemistry differs from the inventory product")
        if selected is not None and (
            product.diameter is None
            or not math.isclose(product.diameter, 1.75, rel_tol=0, abs_tol=1e-6)
        ):
            raise ValueError(
                "a known 1.75 mm product diameter is required for this stock catalogue"
            )
        async with self.journal._lock:
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                current = await self._snapshot(provider_id)
                if current["revision"] != revision:
                    raise InventoryConflict("material selections changed; refresh before editing")
                if selected is None:
                    await self.conn.execute(
                        "DELETE FROM inventory_material_mappings "
                        "WHERE provider_id=? AND product_id=?",
                        (provider_id, product.id),
                    )
                else:
                    await self.conn.execute(
                        "INSERT INTO inventory_material_mappings VALUES (?,?,?,?,?) "
                        "ON CONFLICT(provider_id,product_id) DO UPDATE "
                        "SET stock_key=excluded.stock_key,"
                        "catalogue_digest=excluded.catalogue_digest,product_fingerprint=excluded.product_fingerprint",
                        (
                            provider_id,
                            product.id,
                            selected.key,
                            catalogue_digest(),
                            product_fingerprint(product),
                        ),
                    )
                await self.conn.execute(
                    "INSERT INTO inventory_material_revision VALUES (?,?) "
                    "ON CONFLICT(provider_id) DO UPDATE SET revision=excluded.revision",
                    (provider_id, revision + 1),
                )
                result = await self._snapshot(provider_id)
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
        return result

    async def lookup(self, provider_id: str, product: Product) -> dict[str, Any]:
        async with (
            self.journal._lock,
            self.conn.execute(
                "SELECT * FROM inventory_material_mappings WHERE provider_id=? AND product_id=?",
                (provider_id, product.id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return {"selection": None, "issue": "not_selected"}
        if row["catalogue_digest"] != catalogue_digest():
            return {"selection": None, "issue": "catalogue_changed"}
        if row["product_fingerprint"] != product_fingerprint(product):
            return {"selection": None, "issue": "inventory_product_changed"}
        selected = next((p for p in catalogue() if p.key == row["stock_key"]), None)
        if selected is None:
            return {"selection": None, "issue": "stock_profile_unavailable"}
        return {
            "selection": {
                "key": selected.key,
                "name": selected.name,
                "material_id": selected.material_id,
                "material": selected.material,
                "source_verified": True,
                "runtime_qualified": False,
            },
            "issue": None,
        }
