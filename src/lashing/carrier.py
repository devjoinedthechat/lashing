"""Talking to a carrier over the DCSA standards.

`Carrier` is the port the rest of lashing depends on; `HttpCarrier` implements it for any
DCSA-conformant carrier, including the simulator. Outgoing payloads are validated against the
standard before they are sent, so nothing non-conformant ever leaves. Responses are validated
too: a carrier that drifts from the standard is reported, and in strict mode refused.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from lashing.dcsa.schema import Spec, check, issues

log = logging.getLogger(__name__)

Validation = Literal["strict", "warn", "off"]
MAX_EVENT_PAGES = 20
BOOKING_HEADERS = {"API-Version": "2"}  # Booking 2.x takes the major version only


class CarrierError(Exception):
    """The carrier refused a request or could not be reached."""

    def __init__(self, status: int | None, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or []

    def __str__(self) -> str:
        head = f"carrier returned {self.status}: {self.message}" if self.status else self.message
        return head + ("".join(f"\n  - {d}" for d in self.details) if self.details else "")


class NotFound(CarrierError):
    pass


class Conflict(CarrierError):
    pass


class NotYetProcessed(CarrierError):
    """GET answered 202: the carrier accepted the request but has not processed it yet."""


class NonConformantResponse(CarrierError):
    """The carrier's response does not conform to the DCSA standard (strict mode only)."""


def _segment(reference: str) -> str:
    """A booking reference as one URL path segment: agent input never reshapes the URL."""
    if not reference.strip() or len(reference) > 100:
        raise CarrierError(None, "a booking reference is 1 to 100 characters")
    return quote(reference, safe="")


class Carrier(Protocol):
    async def create_booking(self, payload: dict[str, Any]) -> str: ...

    async def get_booking(self, reference: str, *, amended: bool = False) -> dict[str, Any]: ...

    async def update_booking(self, reference: str, payload: dict[str, Any]) -> None: ...

    async def cancel_booking(self, reference: str, payload: dict[str, Any]) -> None: ...

    async def routes(
        self,
        origin: str,
        destination: str,
        *,
        departure_from: dt.date | None = None,
        departure_until: dt.date | None = None,
        max_transshipments: int = 1,
    ) -> list[dict[str, Any]]: ...

    async def events(
        self,
        *,
        booking_reference: str | None = None,
        equipment_reference: str | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Endpoints:
    """Base URLs of the three APIs; the standard's own paths are appended to each."""

    booking: str
    schedules: str
    tracking: str

    @classmethod
    def under(cls, base: str) -> Endpoints:
        """The layout the simulator uses: /bkg, /cs and /tnt/v3 under one base URL."""
        base = base.rstrip("/")
        return cls(booking=f"{base}/bkg", schedules=f"{base}/cs", tracking=f"{base}/tnt/v3")


@dataclass
class HttpCarrier:
    endpoints: Endpoints
    headers: dict[str, str] = field(default_factory=dict)
    validation: Validation = "warn"
    transport: httpx.AsyncBaseTransport | None = None
    timeout: float = 30.0
    warnings: list[str] = field(default_factory=list)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, headers=self.headers, timeout=self.timeout)

    # -- response handling -----------------------------------------------------------------------

    def _conform(self, spec: Spec, component: str, payload: Any, what: str) -> None:
        if self.validation == "off":
            return
        found = issues(spec, component, payload)
        if not found:
            return
        details = [str(i) for i in found[:10]]
        if self.validation == "strict":
            raise NonConformantResponse(None, f"{what} does not conform to {spec.value} {component}", details)
        note = f"{what} does not conform to {spec.value} {component}: " + "; ".join(details)
        self.warnings.append(note)
        log.warning(note)

    @staticmethod
    def _raise_for(response: httpx.Response, what: str) -> None:
        if response.is_success:
            return
        details: list[str] = []
        message = f"{what} failed"
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            message = str(body.get("statusCodeMessage") or body.get("statusCodeText") or message)
            for error in body.get("errors") or []:
                if isinstance(error, dict):
                    where = f"{error['jsonPath']}: " if error.get("jsonPath") else ""
                    details.append(f"{where}{error.get('errorCodeMessage') or error.get('errorCodeText')}")
            for item in body.get("feedbackElements") or []:
                if isinstance(item, dict):
                    details.append(str(item.get("message")))
        kind = {404: NotFound, 409: Conflict}.get(response.status_code, CarrierError)
        raise kind(response.status_code, message, details)

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._client() as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise CarrierError(None, f"could not reach the carrier at {url}: {error}") from error

    # -- Booking 2.0.5 ---------------------------------------------------------------------------

    async def create_booking(self, payload: dict[str, Any]) -> str:
        check(Spec.BOOKING, "CreateBooking", payload)
        url = f"{self.endpoints.booking}/v2/bookings"
        response = await self._send("POST", url, json=payload, headers=BOOKING_HEADERS)
        self._raise_for(response, "creating the booking")
        body = response.json()
        self._conform(Spec.BOOKING, "CreateBookingResponse", body, "the booking receipt")
        reference = body.get("carrierBookingRequestReference") if isinstance(body, dict) else None
        if not isinstance(reference, str):
            raise NonConformantResponse(response.status_code, "the carrier returned no carrierBookingRequestReference")
        return reference

    async def get_booking(self, reference: str, *, amended: bool = False) -> dict[str, Any]:
        url = f"{self.endpoints.booking}/v2/bookings/{_segment(reference)}"
        params = {"amendedContent": "true"} if amended else None
        response = await self._send("GET", url, params=params, headers=BOOKING_HEADERS)
        if response.status_code == 202:
            raise NotYetProcessed(202, f"the carrier has not processed booking {reference} yet")
        self._raise_for(response, f"reading booking {reference}")
        body = response.json()
        if not isinstance(body, dict):
            raise NonConformantResponse(response.status_code, "the booking is not a JSON object")
        self._conform(Spec.BOOKING, "Booking", body, f"booking {reference}")
        return body

    async def update_booking(self, reference: str, payload: dict[str, Any]) -> None:
        check(Spec.BOOKING, "UpdateBooking", payload)
        url = f"{self.endpoints.booking}/v2/bookings/{_segment(reference)}"
        response = await self._send("PUT", url, json=payload, headers=BOOKING_HEADERS)
        self._raise_for(response, f"changing booking {reference}")

    async def cancel_booking(self, reference: str, payload: dict[str, Any]) -> None:
        check(Spec.BOOKING, "CancelBookingRequest", payload)
        url = f"{self.endpoints.booking}/v2/bookings/{_segment(reference)}"
        response = await self._send("PATCH", url, json=payload, headers=BOOKING_HEADERS)
        self._raise_for(response, f"cancelling booking {reference}")

    # -- Commercial Schedules 1.0.4 ----------------------------------------------------------------

    async def routes(
        self,
        origin: str,
        destination: str,
        *,
        departure_from: dt.date | None = None,
        departure_until: dt.date | None = None,
        max_transshipments: int = 1,
    ) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "placeOfReceipt": origin,
            "placeOfDelivery": destination,
            "maxTranshipment": max_transshipments,
        }
        if departure_from:
            params["departureStartDate"] = departure_from.isoformat()
        if departure_until:
            params["departureEndDate"] = departure_until.isoformat()
        url = f"{self.endpoints.schedules}/v1/point-to-point-routes"
        response = await self._send("GET", url, params=params, headers={"API-Version": "1"})
        self._raise_for(response, "reading schedules")
        body = response.json()
        if not isinstance(body, list):
            raise NonConformantResponse(response.status_code, "point-to-point routes are not a JSON array")
        for n, route in enumerate(body):
            self._conform(Spec.SCHEDULES, "PointToPoint", route, f"route {n + 1}")
        return body

    # -- Track & Trace 3.0.0 ---------------------------------------------------------------------

    async def events(
        self,
        *,
        booking_reference: str | None = None,
        equipment_reference: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if booking_reference:
            params["carrierBookingReference"] = booking_reference
        if equipment_reference:
            params["equipmentReference"] = equipment_reference
        url = f"{self.endpoints.tracking}/events"
        found: list[dict[str, Any]] = []
        for _ in range(MAX_EVENT_PAGES):
            response = await self._send("GET", url, params=params, headers={"API-Version": "3.0.0"})
            self._raise_for(response, "reading tracking events")
            body = response.json()
            self._conform(Spec.TRACKING, "GetEventsResponse", body, "tracking events")
            found.extend(body.get("events", []) if isinstance(body, dict) else [])
            cursor = response.headers.get("Next-Page-Cursor")
            if not cursor:
                break
            params = {"cursor": cursor}
        return found
