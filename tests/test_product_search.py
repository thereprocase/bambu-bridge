"""Product search by words, and Spoolman's catalog as the fallback (owner, 2026-10-23)."""

import json

import httpx

from bambu_bridge.spoolman import CATALOG_TTL_S, SpoolmanClient, matches_words, search_words

VENDOR = {"id": 3, "name": "Polymaker"}
PC = {
    "id": 11,
    "name": "PolyLite PC",
    "vendor": VENDOR,
    "material": "PC",
    "density": 1.2,
    "weight": 1000,
}
PETG = {
    "id": 12,
    "name": "PolyLite™ PETG Galaxy Black",
    "vendor": VENDOR,
    "material": "PETG",
    "density": 1.27,
    "weight": 1000,
}
OTHER = {
    "id": 13,
    "name": "PETG Basic Black",
    "vendor": {"id": 1, "name": "Bambu Lab"},
    "material": "PETG",
    "density": 1.27,
    "weight": 1000,
}
CATALOG = [
    {
        "id": "polymaker_petg_polylitepetgblack_1000_175_c",
        "manufacturer": "Polymaker",
        "name": "PolyLite™ PETG Black",
        "material": "PETG",
        "density": 1.27,
        "weight": 1000,
        "spool_weight": 140,
        "diameter": 1.75,
        "color_hex": "000000",
    },
    {
        "id": "polymaker_petg_polylitepetgblack_1000_285_c",
        "manufacturer": "Polymaker",
        "name": "PolyLite™ PETG Black",
        "material": "PETG",
        "density": 1.27,
        "weight": 1000,
        "diameter": 2.85,
        "color_hex": "000000",
    },
    {
        "id": "bambulab_pla_basic_black",
        "manufacturer": "Bambu Lab",
        "name": "PLA Basic Black",
        "material": "PLA",
        "density": 1.24,
        "weight": 1000,
        "diameter": 1.75,
        "color_hex": "000000",
    },
    {"id": "broken", "manufacturer": "X", "name": "Y", "density": -1},
]


def test_words_ignore_case_and_trademark_marks() -> None:
    assert search_words("Polymaker Petg galaxy black") == ["polymaker", "petg", "galaxy", "black"]
    assert search_words("  ") == []
    assert matches_words(
        ["polymaker", "petg", "black"], "Polymaker", "PolyLite™ PETG Black", "PETG"
    )
    assert not matches_words(["polymaker", "pla"], "Polymaker", "PolyLite™ PETG Black", "PETG")
    assert matches_words([], None)


def _client(handle):
    return httpx.AsyncClient(
        base_url="https://inventory.invalid/api/v1/", transport=httpx.MockTransport(handle)
    )


async def test_product_search_matches_every_word_across_vendor_name_and_material() -> None:
    pages = {0: [PC, PETG, OTHER]}
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(200, json=pages.get(offset, []))

    async with _client(handle) as http:
        client = SpoolmanClient(http)
        # Spoolman's own name filter would return nothing for the vendor alone.
        assert [p.id for p in await client.search_products("Polymaker")] == [11, 12]
        assert [p.id for p in await client.search_products("Polymaker Petg galaxy black")] == [12]
        assert [p.id for p in await client.search_products("petg black")] == [12, 13]
        assert [p.id for p in await client.search_products("petg black", offset=1, limit=1)] == [13]
        assert await client.search_products("polymaker pla") == []
    assert all("name" not in c for c in calls)  # the bridge filters; Spoolman is only paged


async def test_product_search_pages_until_a_short_page() -> None:
    full = [{**OTHER, "id": 100 + i} for i in range(100)]

    def handle(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(200, json=full if offset == 0 else [PETG] if offset == 100 else [])

    async with _client(handle) as http:
        assert [p.id for p in await SpoolmanClient(http).search_products("galaxy")] == [12]


async def test_catalog_matches_words_defaults_to_175_and_caches(monkeypatch) -> None:
    fetches = []

    def handle(request: httpx.Request) -> httpx.Response:
        fetches.append(request.url.path)
        assert request.url.path.endswith("/external/filament")
        return httpx.Response(200, content=json.dumps(CATALOG))

    clock = [1000.0]
    monkeypatch.setattr("bambu_bridge.spoolman.time.monotonic", lambda: clock[0])
    async with _client(handle) as http:
        client = SpoolmanClient(http)
        found = await client.catalog("polymaker petg black")
        assert [e.id for e in found] == [
            "polymaker_petg_polylitepetgblack_1000_175_c"
        ]  # the 2.85 mm twin is left out
        assert (
            found[0].name == "PolyLite™ PETG Black"
            and found[0].weight == 1000
            and found[0].diameter == 1.75
        )
        assert [e.id for e in await client.catalog("black", limit=1)] == [
            "polymaker_petg_polylitepetgblack_1000_175_c"
        ]
        assert len(await client.catalog("")) == 2  # every 1.75 mm entry; the broken row is skipped
        assert len(fetches) == 1  # served from the cache
        clock[0] += CATALOG_TTL_S + 1
        await client.catalog("pla")
        assert len(fetches) == 2  # refreshed after an hour
