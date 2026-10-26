import httpx
import pytest

from bambu_bridge.spoolman import SpoolmanClient, SpoolmanError

SPOOL = {
    "id": 43,
    "filament": {"id": 2, "name": "ASA Black", "weight": 1000},
    "initial_weight": 400,
    "remaining_weight": 350,
}


async def test_partial_spool_and_paginated_search() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[SPOOL] if request.url.path.endswith("/spool") else SPOOL)

    async with httpx.AsyncClient(
        base_url="https://inventory.invalid/api/v1/",
        transport=httpx.MockTransport(handle),
    ) as http:
        client = SpoolmanClient(http)
        spool = await client.get_spool(43)
        assert spool.remaining_percent == 35  # not 350/400
        assert await client.search_spools(" ASA ", offset=50) == [spool]
    assert requests[1].url.params["filament.name"] == "ASA"
    assert requests[1].url.params["offset"] == "50"
    assert all(r.method == "GET" for r in requests)


@pytest.mark.parametrize(
    "payload",
    [
        {**SPOOL, "id": 44},
        {**SPOOL, "remaining_weight": -1},
        {**SPOOL, "remaining_weight": float("inf")},
        {"id": 43},
    ],
)
async def test_bad_provider_data(payload: dict) -> None:
    import json

    async with httpx.AsyncClient(
        base_url="https://inventory.invalid/api/v1/",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=json.dumps(payload))),
    ) as http:
        with pytest.raises(SpoolmanError, match="invalid_response"):
            await SpoolmanClient(http).get_spool(43)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "authentication"),
        (403, "authentication"),
        (404, "not_found"),
        (500, "provider_error"),
        (302, "provider_error"),
    ],
)
async def test_provider_errors_and_no_redirect(status: int, kind: str) -> None:
    async with httpx.AsyncClient(
        base_url="https://inventory.invalid/api/v1/",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                status,
                headers={"location": "https://unrelated.invalid/"},
                text="private details",
            )
        ),
    ) as http:
        with pytest.raises(SpoolmanError, match=kind) as failure:
            await SpoolmanClient(http).get_spool(43)
        assert "private" not in str(failure.value)


async def test_timeout_is_distinct() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret URL", request=request)

    async with httpx.AsyncClient(
        base_url="https://inventory.invalid/api/v1/",
        transport=httpx.MockTransport(handle),
    ) as http:
        with pytest.raises(SpoolmanError, match="timeout"):
            await SpoolmanClient(http).get_spool(43)


async def test_success_with_wrong_weight_is_an_uncertain_write() -> None:
    from bambu_bridge.spoolman import ProviderWriteError

    async with httpx.AsyncClient(
        base_url="https://inventory.invalid/api/v1/",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=SPOOL)),
    ) as http:
        with pytest.raises(ProviderWriteError) as failure:
            await SpoolmanClient(http).write(
                "set_remaining", {"spool_id": 43, "remaining_weight": 100}
            )
        assert failure.value.outcome == "uncertain"


def test_percent_rounding_and_extreme_overcapacity() -> None:
    from bambu_bridge.spoolman import ProviderSpool

    assert (
        ProviderSpool.model_validate(
            {
                **SPOOL,
                "remaining_weight": 425,
            }
        ).remaining_percent
        == 43
    )
    assert (
        ProviderSpool.model_validate(
            {
                **SPOOL,
                "filament": {"id": 2, "weight": 1e-308},
                "remaining_weight": 1e308,
            }
        ).remaining_percent
        == 100
    )


def test_record_fields_pass_through_and_stay_absent_when_spoolman_omits_them() -> None:
    from pydantic import ValidationError

    from bambu_bridge.spoolman import ProviderSpool

    full = ProviderSpool.model_validate(
        {
            **SPOOL,
            "lot_nr": "A7533841419",
            "location": "Shelf B",
            "price": 24.9,
            "first_used": "2026-10-20T08:00:00Z",
            "last_used": "2026-10-26T13:25:29Z",
            "filament": {**SPOOL["filament"], "article_number": "PD01011", "price": 22.5},
        }
    )
    dumped = full.model_dump(mode="json")
    assert dumped["lot_nr"] == "A7533841419"
    assert dumped["location"] == "Shelf B"
    assert dumped["price"] == 24.9
    assert dumped["first_used"] == "2026-10-20T08:00:00Z"
    assert dumped["last_used"] == "2026-10-26T13:25:29Z"
    assert dumped["filament"]["article_number"] == "PD01011"
    assert dumped["filament"]["price"] == 22.5

    bare = ProviderSpool.model_validate(SPOOL).model_dump(mode="json")
    for key in ("lot_nr", "location", "price", "first_used", "last_used"):
        assert bare[key] is None
    assert bare["filament"]["article_number"] is None
    assert bare["filament"]["price"] is None
    with pytest.raises(ValidationError):
        ProviderSpool.model_validate({**SPOOL, "price": -1})
