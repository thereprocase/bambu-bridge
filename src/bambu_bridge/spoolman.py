"""Spoolman v0.22.1 adapter; writes must be called through InventoryService.

The caller owns the HTTP client, authentication, and server-side base URL.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class SpoolmanError(Exception):
    """Provider failure without leaking its URL, credentials or response body."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status


class ProviderEntity(BaseModel):
    id: int = Field(gt=0, strict=True)


T = TypeVar("T", bound=ProviderEntity)


class Vendor(ProviderEntity):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    id: int = Field(gt=0, strict=True)
    comment: str | None = None
    name: str
    empty_spool_weight: float | None = Field(default=None, ge=0)


class Product(ProviderEntity):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    id: int = Field(gt=0, strict=True)
    comment: str | None = None
    name: str | None = None
    vendor: Vendor | None = None
    material: str | None = None
    density: float | None = Field(default=None, gt=0)
    diameter: float | None = Field(default=None, gt=0)
    color_hex: str | None = None
    weight: float | None = Field(default=None, gt=0)
    spool_weight: float | None = Field(default=None, ge=0)


class ProviderSpool(ProviderEntity):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    id: int = Field(gt=0, strict=True)
    comment: str | None = None
    filament: Product
    remaining_weight: float | None = Field(default=None, ge=0)
    # Spoolman always reports it; `use` moves it even when remaining is unknown.
    used_weight: float | None = Field(default=None, ge=0)
    initial_weight: float | None = Field(default=None, ge=0)
    spool_weight: float | None = Field(default=None, ge=0)
    archived: bool = False

    @property
    def remaining_percent(self) -> int | None:
        # Package capacity, not the initial weight of a partially used spool.
        nominal = self.filament.weight
        if nominal is None or self.remaining_weight is None:
            return None
        return math.floor(min(1.0, self.remaining_weight / nominal) * 100 + 0.5)


class ProviderWriteError(SpoolmanError):
    def __init__(self, kind: str, *, outcome: str, status: int | None = None) -> None:
        super().__init__(kind, status)
        self.outcome = outcome  # not_sent, rejected, or uncertain


def search_words(query: str) -> list[str]:
    """The words of a search, lowercased, with trademark marks and punctuation dropped."""
    cleaned = re.sub(r"[™®©]", "", query.lower())
    return [w for w in re.split(r"[^a-z0-9.+-]+", cleaned) if w]


def matches_words(words: list[str], *fields: str | None) -> bool:
    """Every word appears somewhere in the fields, case-insensitive, marks ignored."""
    haystack = " ".join(re.sub(r"[™®©]", "", f.lower()) for f in fields if f)
    return all(w in haystack for w in words)


class CatalogEntry(ProviderEntity):
    """One filament of Spoolman's built-in catalog (GET external/filament)."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    id: str
    manufacturer: str | None = None
    name: str | None = None
    material: str | None = None
    density: float | None = Field(default=None, gt=0)
    weight: float | None = Field(default=None, gt=0)
    spool_weight: float | None = Field(default=None, ge=0)
    diameter: float | None = Field(default=None, gt=0)
    color_hex: str | None = None


CATALOG_TTL_S = 3600.0
SEARCH_PAGE = 100
SEARCH_PAGES = 20  # 2000 filaments: well past a home inventory


class SpoolmanClient:
    """A client whose base URL ends in /api/v1/; never follows redirects."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._catalog: tuple[float, list[CatalogEntry]] | None = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> object:
        try:
            response = await self._client.get(path, params=params, follow_redirects=False)
        except httpx.TimeoutException:
            raise SpoolmanError("timeout") from None
        except httpx.RequestError:
            raise SpoolmanError("unavailable") from None
        if response.status_code in (401, 403):
            raise SpoolmanError("authentication", response.status_code)
        if response.status_code == 404:
            raise SpoolmanError("not_found", 404)
        if not response.is_success:
            raise SpoolmanError("provider_error", response.status_code)
        try:
            return response.json()
        except ValueError:
            raise SpoolmanError("invalid_response") from None

    async def get_spool(self, spool_id: int) -> ProviderSpool:
        if isinstance(spool_id, bool) or not isinstance(spool_id, int) or spool_id <= 0:
            raise ValueError("spool_id must be a positive integer")
        payload = await self._get(f"spool/{spool_id}")
        try:
            spool = ProviderSpool.model_validate(payload)
            if spool.id != spool_id:
                raise SpoolmanError("invalid_response")
            return spool
        except ValidationError:
            raise SpoolmanError("invalid_response") from None

    async def search_spools(
        self,
        query: str = "",
        *,
        offset: int = 0,
        limit: int = 50,
        include_archived: bool = False,
        material: str = "",
    ) -> list[ProviderSpool]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid page")
        params = {
            "offset": offset,
            "limit": limit,
            "allow_archived": str(include_archived).lower(),
            "sort": "id:asc",
        }
        if query.strip():
            params["filament.name"] = query.strip()
        if material.strip():
            params["filament.material"] = material.strip()
        payload = await self._get("spool", params)
        if not isinstance(payload, list) or len(payload) > limit:
            raise SpoolmanError("invalid_response")
        try:
            return [ProviderSpool.model_validate(item) for item in payload]
        except ValidationError:
            raise SpoolmanError("invalid_response") from None

    async def get_product(self, product_id: int) -> Product:
        try:
            product = Product.model_validate(await self._get(f"filament/{product_id}"))
        except ValidationError:
            raise SpoolmanError("invalid_response") from None
        if product.id != product_id:
            raise SpoolmanError("invalid_response")
        return product

    async def search_products(
        self, query: str = "", *, offset: int = 0, limit: int = 50
    ) -> list[Product]:
        """Products whose vendor, name or material carry every word of the query.

        Spoolman's own filter takes the query as one substring of the filament
        name, so "Polymaker" finds nothing; the bridge pages through the list
        and matches word by word instead. A home inventory is a few pages.
        """
        words = search_words(query)
        if not words:
            return await self._search(
                "filament", {"offset": offset, "limit": limit, "sort": "id:asc"}, Product, limit
            )
        found: list[Product] = []
        skipped = 0
        for page in range(SEARCH_PAGES):
            batch = await self._search(
                "filament",
                {"offset": page * SEARCH_PAGE, "limit": SEARCH_PAGE, "sort": "id:asc"},
                Product,
                SEARCH_PAGE,
            )
            for product in batch:
                vendor = product.vendor.name if product.vendor else None
                if not matches_words(
                    words, vendor, product.name, product.material, product.comment
                ):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                found.append(product)
                if len(found) >= limit:
                    return found
            if len(batch) < SEARCH_PAGE:
                break
        return found

    async def catalog(
        self, query: str, *, limit: int = 30, diameter: float = 1.75
    ) -> list[CatalogEntry]:
        """Spoolman's built-in filament catalog, matched word by word; cached for an hour."""
        now = time.monotonic()
        if self._catalog is None or now - self._catalog[0] > CATALOG_TTL_S:
            payload = await self._get("external/filament")
            if not isinstance(payload, list):
                raise SpoolmanError("invalid_response")
            entries: list[CatalogEntry] = []
            for item in payload:
                try:
                    entries.append(CatalogEntry.model_validate(item))
                except ValidationError:
                    continue  # one odd catalog row never hides the rest
            self._catalog = (now, entries)
        words = search_words(query)
        found: list[CatalogEntry] = []
        for entry in self._catalog[1]:
            if entry.diameter is not None and not math.isclose(
                entry.diameter, diameter, abs_tol=0.01
            ):
                continue
            if words and not matches_words(words, entry.manufacturer, entry.name, entry.material):
                continue
            found.append(entry)
            if len(found) >= limit:
                break
        return found

    async def search_vendors(
        self, query: str = "", *, offset: int = 0, limit: int = 50
    ) -> list[Vendor]:
        params = {"offset": offset, "limit": limit, "sort": "id:asc"}
        if query.strip():
            params["name"] = query.strip()
        return await self._search("vendor", params, Vendor, limit)

    async def _search(
        self, path: str, params: dict[str, Any], model: type[T], limit: int
    ) -> list[T]:
        if not 1 <= limit <= 100 or params["offset"] < 0:
            raise ValueError("invalid page")
        payload = await self._get(path, params)
        if not isinstance(payload, list) or len(payload) > limit:
            raise SpoolmanError("invalid_response")
        try:
            return [model.model_validate(item) for item in payload]
        except ValidationError:
            raise SpoolmanError("invalid_response") from None

    async def write(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send once. Every failure carries its conservative delivery outcome."""
        routes: dict[str, tuple[str, str, type[ProviderEntity]]] = {
            "create_vendor": ("POST", "vendor", Vendor),
            "create_product": ("POST", "filament", Product),
            "create_spool": ("POST", "spool", ProviderSpool),
            "set_remaining": ("PATCH", "spool/{spool_id}", ProviderSpool),
            "measure": ("PUT", "spool/{spool_id}/measure", ProviderSpool),
            "archive": ("PATCH", "spool/{spool_id}", ProviderSpool),
            "consume": ("PUT", "spool/{spool_id}/use", ProviderSpool),
            "set_vendor_empty_weight": ("PATCH", "vendor/{vendor_id}", Vendor),
        }
        method, path, model = routes[kind]
        body = dict(payload)
        spool_id = body.pop("spool_id", None)
        if kind == "set_vendor_empty_weight":
            # create_product also carries a vendor_id, but as data, so only this kind pops it.
            spool_id = body.pop("vendor_id")  # checked against the echoed id below
        path = path.format(spool_id=spool_id, vendor_id=spool_id)
        try:
            response = await self._client.request(method, path, json=body, follow_redirects=False)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise ProviderWriteError("unavailable", outcome="not_sent") from None
        except httpx.RequestError:
            raise ProviderWriteError("delivery_unknown", outcome="uncertain") from None
        if response.status_code in (400, 401, 403, 404, 405, 409, 422):
            raise ProviderWriteError("rejected", outcome="rejected", status=response.status_code)
        if not response.is_success:
            raise ProviderWriteError(
                "delivery_unknown", outcome="uncertain", status=response.status_code
            )
        try:
            result = model.model_validate(response.json())
        except (ValidationError, ValueError):
            raise ProviderWriteError("invalid_response", outcome="uncertain") from None
        if spool_id is not None and result.id != spool_id:
            raise ProviderWriteError("invalid_response", outcome="uncertain")
        if (
            kind == "create_spool"
            and isinstance(result, ProviderSpool)
            and result.filament.id != body.get("filament_id")
        ):
            raise ProviderWriteError("unexpected_product", outcome="uncertain")
        expected_weight = body.get("remaining_weight")
        if (
            expected_weight is not None
            and isinstance(result, ProviderSpool)
            and (
                result.remaining_weight is None
                or not math.isclose(
                    result.remaining_weight,
                    expected_weight,
                    rel_tol=0,
                    abs_tol=1e-6,
                )
            )
        ):
            raise ProviderWriteError("unexpected_weight", outcome="uncertain")
        if kind == "archive" and isinstance(result, ProviderSpool) and not result.archived:
            raise ProviderWriteError("unexpected_archive_state", outcome="uncertain")
        # A measured bare-spool weight must come back as sent, or the owner is told it is unsure.
        sent = body.get("spool_weight") if kind == "archive" else body.get("empty_spool_weight")
        echoed = getattr(
            result, "spool_weight" if kind == "archive" else "empty_spool_weight", None
        )
        if sent is not None and (echoed is None or not math.isclose(echoed, sent, abs_tol=1e-6)):
            raise ProviderWriteError("unexpected_weight", outcome="uncertain")
        return result.model_dump(mode="json")

    async def get_vendor(self, vendor_id: int) -> Vendor:
        try:
            vendor = Vendor.model_validate(await self._get(f"vendor/{vendor_id}"))
        except ValidationError:
            raise SpoolmanError("invalid_response") from None
        if vendor.id != vendor_id:
            raise SpoolmanError("invalid_response")
        return vendor

    async def health(self) -> dict[str, Any]:
        health = await self._get("health")
        info = await self._get("info")
        if not isinstance(health, dict) or health.get("status") != "healthy":
            raise SpoolmanError("unavailable")
        if not isinstance(info, dict) or not isinstance(info.get("version"), str):
            raise SpoolmanError("invalid_response")
        # Do not relay provider filesystem paths or other internal configuration.
        return {"healthy": True, "version": info["version"], "tested_version": "0.22.1"}
