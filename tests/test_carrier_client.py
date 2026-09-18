"""The DCSA HTTP client against carriers that misbehave."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from lashing.carrier import (
    CarrierError,
    Conflict,
    Endpoints,
    HttpCarrier,
    NonConformantResponse,
    NotFound,
    NotYetProcessed,
)
from lashing.dcsa.schema import SchemaViolation

pytestmark = pytest.mark.anyio
FIXTURES = Path(__file__).parent / "fixtures" / "dcsa"


def carrier(handler: Any, validation: str = "warn") -> HttpCarrier:
    return HttpCarrier(
        Endpoints.under("http://carrier.example"),
        validation=validation,  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )


def never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"nothing should have been sent, but {request.method} {request.url} was")


async def test_a_non_conformant_request_is_never_sent() -> None:
    with pytest.raises(SchemaViolation):
        await carrier(never_called).create_booking({"requestedEquipments": []})
    with pytest.raises(SchemaViolation):
        await carrier(never_called).cancel_booking("CBR1", {"reason": "no target"})


@pytest.mark.parametrize(("mode", "raises"), [("strict", True), ("warn", False), ("off", False)])
async def test_non_conformant_responses_follow_the_validation_mode(mode: str, raises: bool) -> None:
    def drifted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bookingStatus": "CONFIRMED", "carrierBookingReference": "X"})

    client = carrier(drifted, mode)
    if raises:
        with pytest.raises(NonConformantResponse):
            await client.get_booking("X")
    else:
        assert (await client.get_booking("X"))["bookingStatus"] == "CONFIRMED"
        assert bool(client.warnings) is (mode == "warn")


@pytest.mark.parametrize(("status", "kind"), [(404, NotFound), (409, Conflict), (500, CarrierError)])
async def test_error_responses_keep_the_carriers_details(status: int, kind: type[CarrierError]) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        body = {
            "statusCodeText": "Nope",
            "errors": [{"errorCodeText": "x", "errorCodeMessage": "units too high", "jsonPath": "$.units"}],
        }
        return httpx.Response(status, json=body)

    with pytest.raises(kind) as caught:
        await carrier(failing).get_booking("X")
    assert caught.value.details == ["$.units: units too high"]


async def test_a_202_on_read_means_not_processed_yet() -> None:
    with pytest.raises(NotYetProcessed):
        await carrier(lambda r: httpx.Response(202)).get_booking("cbrr-1")


async def test_an_unreachable_carrier_is_a_carrier_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(CarrierError, match="could not reach the carrier"):
        await carrier(down).routes("CNSHA", "NLRTM")


async def test_requests_carry_the_api_version_each_standard_asks_for() -> None:
    seen: dict[str, str] = {}

    def record(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers.get("API-Version", "")
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"events": []})
        if request.url.path.endswith("/point-to-point-routes"):
            return httpx.Response(200, json=[])
        return httpx.Response(202, json={"carrierBookingRequestReference": "cbrr-1"})

    client = carrier(record)
    await client.create_booking(json.loads((FIXTURES / "booking-dry-cargo.json").read_text()))
    await client.routes("CNSHA", "NLRTM")
    await client.events(booking_reference="CBR1")
    assert seen == {"/bkg/v2/bookings": "2", "/cs/v1/point-to-point-routes": "1", "/tnt/v3/events": "3.0.0"}


async def test_tracking_follows_the_next_page_cursor() -> None:
    pages = {None: (["a", "b"], "p2"), "p2": (["c"], None)}

    def paged(request: httpx.Request) -> httpx.Response:
        events, cursor = pages[request.url.params.get("cursor")]
        headers = {"Next-Page-Cursor": cursor} if cursor else {}
        return httpx.Response(200, json={"events": [{"eventID": e} for e in events]}, headers=headers)

    found = await carrier(paged, "off").events(booking_reference="CBR1")
    assert [e["eventID"] for e in found] == ["a", "b", "c"]


async def test_references_cannot_reshape_the_url() -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(404, json={})

    for sneaky in ("X/../../admin", "X?amendedContent=true", "X#frag"):
        with pytest.raises(NotFound):
            await carrier(record).get_booking(sneaky)
    assert seen == [
        "/bkg/v2/bookings/X%2F..%2F..%2Fadmin",
        "/bkg/v2/bookings/X%3FamendedContent%3Dtrue",
        "/bkg/v2/bookings/X%23frag",
    ]
    with pytest.raises(CarrierError, match="1 to 100 characters"):
        await carrier(never_called).get_booking(" ")
