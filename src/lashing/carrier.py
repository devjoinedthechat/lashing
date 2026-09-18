"""Talking to a carrier over the DCSA standards.

`Carrier` is the port the rest of lashing depends on; `HttpCarrier` implements it for any
DCSA-conformant carrier, including the simulator. Outgoing payloads are validated against the
standard before they are sent, so nothing non-conformant ever leaves. Responses are validated
too: a carrier that drifts from the standard is reported, and in strict mode refused.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import quote

import anyio
import httpx

from lashing.dcsa.schema import Spec, check, issues
from lashing.views import carrier_text

log = logging.getLogger(__name__)

Validation = Literal["strict", "warn", "off"]
MAX_EVENT_PAGES = 20
BOOKING_HEADERS = {"API-Version": "2"}  # Booking 2.x takes the major version only
MAX_RESPONSE_BYTES = 5_000_000
MAX_RETRY_WAIT = 10.0
RETRYABLE_READ_STATUSES = frozenset({429, 502, 503, 504})


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
    """The carrier's response does not conform to the DCSA standard, or is not usable at all."""


class NotSent(CarrierError):
    """The request never reached the carrier. Sending it again is safe."""


class OutcomeUnknown(CarrierError):
    """The request may have reached the carrier. It must not be sent again until someone has checked."""


def _segment(reference: str) -> str:
    """A booking reference as one URL path segment: agent input never reshapes the URL."""
    if not reference.strip() or len(reference) > 100 or reference != reference.strip():
        raise CarrierError(None, "a booking reference is 1 to 100 characters with no surrounding spaces")
    if set(reference) == {"."}:
        raise CarrierError(None, "a booking reference cannot be only dots")
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
    ) -> Events: ...


@dataclass(frozen=True)
class Events:
    events: list[dict[str, Any]]
    truncated: bool = False  # the carrier had more pages than lashing reads


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
    """A DCSA carrier over HTTP, with one pooled connection shared by every call.

    Reads are retried with backoff on transient failures. Writes never are: a write that fails
    before any byte left raises `NotSent` (safe to try again); one that may have reached the
    carrier raises `OutcomeUnknown` (someone must check before it is sent again).
    """

    endpoints: Endpoints
    headers: dict[str, str] = field(default_factory=dict, repr=False)  # carries credentials
    validation: Validation = "warn"
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    timeout: float = 30.0
    read_retries: int = 2
    backoff: float = 0.5
    warnings: deque[str] = field(default_factory=lambda: deque(maxlen=100), repr=False)
    _http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                transport=self.transport,
                headers=self.headers,
                timeout=self.timeout,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                follow_redirects=False,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

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
    def _json(response: httpx.Response, what: str) -> Any:
        try:
            return response.json()
        except ValueError:
            raise NonConformantResponse(response.status_code, f"{what}: the carrier did not answer with JSON") from None

    @staticmethod
    def _problem(response: httpx.Response, what: str) -> tuple[str, list[str]]:
        """The carrier's explanation of a failure: its text is untrusted, so it is cleaned and capped."""
        details: list[str] = []
        message = f"{what} failed"
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            said = body.get("statusCodeMessage") or body.get("statusCodeText")
            if said:
                message = f"{what} failed; the carrier says: {carrier_text(said, 200)}"
            for error in (body.get("errors") or [])[:10]:
                if isinstance(error, dict):
                    where = f"{carrier_text(error['jsonPath'], 100)}: " if error.get("jsonPath") else ""
                    text = error.get("errorCodeMessage") or error.get("errorCodeText") or ""
                    details.append(where + carrier_text(text, 300))
            for item in (body.get("feedbackElements") or [])[:10]:
                if isinstance(item, dict):
                    details.append(carrier_text(item.get("message", ""), 300))
        return message, details

    def _raise_for(self, response: httpx.Response, what: str) -> None:
        if response.is_success:
            return
        message, details = self._problem(response, what)
        kind = {404: NotFound, 409: Conflict}.get(response.status_code, CarrierError)
        raise kind(response.status_code, message, details)

    # -- transport -------------------------------------------------------------------------------

    async def _once(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One request, with the response body capped so a misbehaving carrier cannot exhaust memory."""
        async with self._client().stream(method, url, **kwargs) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise NonConformantResponse(
                        response.status_code,
                        f"the carrier's response to {method} {url} is larger than {MAX_RESPONSE_BYTES:,} bytes",
                    )
            headers = [
                (k, v)
                for k, v in response.headers.multi_items()
                if k.lower() not in ("content-encoding", "content-length")
            ]
            return httpx.Response(response.status_code, headers=headers, content=bytes(body), request=response.request)

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.strip().isdigit():
            return min(float(retry_after), MAX_RETRY_WAIT)
        jitter = random.uniform(0, self.backoff)  # noqa: S311 - spreading retries, not security
        return min(self.backoff * float(2**attempt) + jitter, MAX_RETRY_WAIT)

    async def _read(self, url: str, **kwargs: Any) -> httpx.Response:
        """A GET, retried on connection failures, timeouts, 429 and 502-504."""
        for attempt in range(self.read_retries + 1):
            last = attempt == self.read_retries
            try:
                response = await self._once("GET", url, **kwargs)
            except httpx.DecodingError as error:
                raise NonConformantResponse(None, f"the carrier's response could not be decoded: {error}") from error
            except httpx.TransportError as error:
                if last:
                    raise CarrierError(None, f"could not reach the carrier at {url}: {type(error).__name__}") from error
                await anyio.sleep(self._delay(attempt, None))
                continue
            if response.status_code in RETRYABLE_READ_STATUSES and not last:
                await anyio.sleep(self._delay(attempt, response.headers.get("Retry-After")))
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    async def _write(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """A POST, PUT or PATCH, sent once. Failures say whether the carrier could have received it."""
        try:
            response = await self._once(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol) as error:
            raise NotSent(
                None, f"could not reach the carrier at {url} ({type(error).__name__}); nothing was sent"
            ) from error
        except (httpx.TransportError, httpx.DecodingError, NonConformantResponse) as error:
            raise OutcomeUnknown(
                None,
                f"the exchange failed after the request was sent ({type(error).__name__}); "
                "the carrier may or may not have received it",
            ) from error
        if response.status_code >= 500 or response.status_code == 408:
            message, details = self._problem(response, f"{method} {url}")
            raise OutcomeUnknown(response.status_code, message + "; the carrier may or may not have acted", details)
        return response

    # -- Booking 2.0.5 ---------------------------------------------------------------------------

    async def create_booking(self, payload: dict[str, Any]) -> str:
        check(Spec.BOOKING, "CreateBooking", payload)
        url = f"{self.endpoints.booking}/v2/bookings"
        response = await self._write("POST", url, json=payload, headers=BOOKING_HEADERS)
        self._raise_for(response, "creating the booking")
        try:
            body = self._json(response, "creating the booking")
            self._conform(Spec.BOOKING, "CreateBookingResponse", body, "the booking receipt")
        except NonConformantResponse as error:
            body, problem = None, str(error)
        else:
            problem = "it returned no carrierBookingRequestReference"
        reference = body.get("carrierBookingRequestReference") if isinstance(body, dict) else None
        if not isinstance(reference, str) or not reference.strip():
            # The carrier accepted the request (2xx), so the booking may well exist; we just cannot name it.
            raise OutcomeUnknown(response.status_code, f"the carrier accepted the booking but {problem}")
        return reference

    async def get_booking(self, reference: str, *, amended: bool = False) -> dict[str, Any]:
        url = f"{self.endpoints.booking}/v2/bookings/{_segment(reference)}"
        params = {"amendedContent": "true"} if amended else None
        response = await self._read(url, params=params, headers=BOOKING_HEADERS)
        if response.status_code == 202:
            raise NotYetProcessed(202, f"the carrier has not processed booking {reference} yet")
        self._raise_for(response, f"reading booking {reference}")
        body = self._json(response, f"reading booking {reference}")
        if not isinstance(body, dict):
            raise NonConformantResponse(response.status_code, "the booking is not a JSON object")
        self._conform(Spec.BOOKING, "Booking", body, f"booking {reference}")
        return body

    async def update_booking(self, reference: str, payload: dict[str, Any]) -> None:
        check(Spec.BOOKING, "UpdateBooking", payload)
        url = f"{self.endpoints.booking}/v2/bookings/{_segment(reference)}"
        response = await self._write("PUT", url, json=payload, headers=BOOKING_HEADERS)
        self._raise_for(response, f"changing booking {reference}")

    async def cancel_booking(self, reference: str, payload: dict[str, Any]) -> None:
        check(Spec.BOOKING, "CancelBookingRequest", payload)
        url = f"{self.endpoints.booking}/v2/bookings/{_segment(reference)}"
        response = await self._write("PATCH", url, json=payload, headers=BOOKING_HEADERS)
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
        response = await self._read(url, params=params, headers={"API-Version": "1"})
        self._raise_for(response, "reading schedules")
        body = self._json(response, "reading schedules")
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
    ) -> Events:
        params: dict[str, str] = {}
        if booking_reference:
            params["carrierBookingReference"] = booking_reference
        if equipment_reference:
            params["equipmentReference"] = equipment_reference
        url = f"{self.endpoints.tracking}/events"
        found: list[dict[str, Any]] = []
        for _ in range(MAX_EVENT_PAGES):
            response = await self._read(url, params=params, headers={"API-Version": "3.0.0"})
            self._raise_for(response, "reading tracking events")
            body = self._json(response, "reading tracking events")
            self._conform(Spec.TRACKING, "GetEventsResponse", body, "tracking events")
            found.extend(body.get("events", []) if isinstance(body, dict) else [])
            cursor = response.headers.get("Next-Page-Cursor")
            if not cursor:
                return Events(found)
            params = {"cursor": cursor}
        log.warning("stopped reading tracking events after %d pages", MAX_EVENT_PAGES)
        return Events(found, truncated=True)
