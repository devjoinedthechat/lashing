"""The simulator over HTTP: DCSA Booking 2.0.5, Commercial Schedules 1.0.4 and Track & Trace 3.0.0.

Mount points follow the standards' own paths under a prefix per API (`/bkg`, `/cs`, `/tnt/v3`), so
lashing's HTTP client talks to this app exactly as it would to a carrier. `/_sim` holds scenario
controls, which are not part of any standard.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
from http import HTTPStatus
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from lashing.dcsa.schema import Spec, issues
from lashing.sim import Simulator
from lashing.sim.desk import DeskError, iso

BOOKING_VERSION = "2.0.5"
SCHEDULES_VERSION = "1.0.4"
TRACKING_VERSION = "3.0.0"


def _detail(text: str, message: str, json_path: str | None = None) -> dict[str, str]:
    detail = {"errorCodeText": text[:100], "errorCodeMessage": message[:5000]}
    if json_path:
        detail["jsonPath"] = json_path[:500]
    return detail


async def _json(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _utc_day(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.UTC)


class CarrierApi:
    """HTTP handlers over one simulator."""

    def __init__(self, sim: Simulator) -> None:
        self.sim = sim

    # -- helpers ---------------------------------------------------------------------------------

    def _error(self, request: Request, status: int, errors: list[dict[str, str]], version: str) -> JSONResponse:
        body = {
            "httpMethod": request.method,
            "requestUri": request.url.path,
            "statusCode": status,
            "statusCodeText": HTTPStatus(status).phrase[:50],
            "errorDateTime": iso(self.sim.now),
            "errors": errors,
        }
        return JSONResponse(body, status_code=status, headers={"API-Version": version})

    @staticmethod
    def _ok(body: Any, status: int = 200, version: str = BOOKING_VERSION) -> Response:
        if body is None:
            return Response(status_code=status, headers={"API-Version": version})
        return JSONResponse(body, status_code=status, headers={"API-Version": version})

    def _invalid(self, request: Request, component: str, payload: Any) -> JSONResponse | None:
        if payload is None:
            detail = _detail("Malformed JSON", "The request body is not valid JSON.")
            return self._error(request, 400, [detail], BOOKING_VERSION)
        found = issues(Spec.BOOKING, component, payload)
        if found:
            details = [_detail("Schema violation", issue.message, issue.path) for issue in found[:20]]
            return self._error(request, 400, details, BOOKING_VERSION)
        return None

    def _settle(self) -> None:
        if self.sim.desk.auto_process:
            self.sim.desk.process()

    # -- Booking 2.0.5 ---------------------------------------------------------------------------

    async def create_booking(self, request: Request) -> Response:
        self._settle()
        payload = await _json(request)
        if (problem := self._invalid(request, "CreateBooking", payload)) is not None:
            return problem
        reference = self.sim.desk.submit(payload)
        return self._ok({"carrierBookingRequestReference": reference}, 202)

    async def booking(self, request: Request) -> Response:
        self._settle()
        reference = request.path_params["reference"]
        try:
            if request.method == "GET":
                amended = request.query_params.get("amendedContent", "false").lower() == "true"
                return self._ok(self.sim.desk.view(reference, amended=amended))
            payload = await _json(request)
            component = "UpdateBooking" if request.method == "PUT" else "CancelBookingRequest"
            if (problem := self._invalid(request, component, payload)) is not None:
                return problem
            if request.method == "PUT":
                self.sim.desk.change(reference, payload)
            else:
                self.sim.desk.cancel(reference, payload)
            return self._ok(None, 202)
        except DeskError as error:
            detail = _detail(HTTPStatus(error.status).phrase, error.message)
            return self._error(request, error.status, [detail], BOOKING_VERSION)

    # -- Commercial Schedules 1.0.4 ----------------------------------------------------------------

    async def routes(self, request: Request) -> Response:
        query = request.query_params
        origin, destination = query.get("placeOfReceipt"), query.get("placeOfDelivery")
        if not origin or not destination:
            detail = _detail("Missing parameter", "placeOfReceipt and placeOfDelivery are required.")
            return self._error(request, 400, [detail], SCHEDULES_VERSION)
        try:
            earliest = _utc_day(query["departureStartDate"]) if "departureStartDate" in query else self.sim.now
            end = query.get("departureEndDate")
            latest = _utc_day(end) + dt.timedelta(days=1) if end else earliest + dt.timedelta(days=21)
            transshipments = int(query.get("maxTranshipment", 1))
            limit = int(query.get("limit", 100))
        except ValueError:
            detail = _detail("Invalid parameter", "Dates must be YYYY-MM-DD; maxTranshipment and limit integers.")
            return self._error(request, 400, [detail], SCHEDULES_VERSION)
        found = self.sim.point_to_point(origin, destination, earliest, latest, transshipments)
        return self._ok(found[:limit], version=SCHEDULES_VERSION)

    # -- Track & Trace 3.0.0 ---------------------------------------------------------------------

    async def events(self, request: Request) -> Response:
        self._settle()
        query = request.query_params
        try:
            updated = {
                name: dt.datetime.fromisoformat(query[param].replace("Z", "+00:00"))
                for name, param in (
                    ("updated_min", "eventUpdatedDateTimeMin"),
                    ("updated_max", "eventUpdatedDateTimeMax"),
                )
                if param in query
            }
            start = int(base64.urlsafe_b64decode(query["cursor"]).decode()) if "cursor" in query else 0
            limit = int(query.get("limit", 100))
        except (ValueError, binascii.Error):
            feedback = {"severity": "ERROR", "code": "INVALID_PARAMETER", "message": "Invalid query parameter."}
            return JSONResponse({"feedbackElements": [feedback]}, status_code=400)
        types = {t for t in query.get("eventTypes", "").split(",") if t}
        found = self.sim.tracker.events(
            booking_reference=query.get("carrierBookingReference"),
            equipment_reference=query.get("equipmentReference"),
            event_types=types or None,
            **updated,
        )
        if query.get("transportDocumentReference"):
            found = []  # the simulator issues no transport documents
        headers = {"API-Version": TRACKING_VERSION}
        if start + limit < len(found):
            headers["Next-Page-Cursor"] = base64.urlsafe_b64encode(str(start + limit).encode()).decode()
        return JSONResponse({"events": found[start : start + limit]}, headers=headers)

    # -- scenario controls -------------------------------------------------------------------------

    async def state(self, _: Request) -> Response:
        bookings = [
            {
                "carrierBookingRequestReference": b.request_reference,
                "carrierBookingReference": b.booking_reference,
                "bookingStatus": b.status.value,
                "amendedBookingStatus": b.amendment.value if b.amendment else None,
                "bookingCancellationStatus": b.cancellation.value if b.cancellation else None,
                "route": b.route.reference if b.route else None,
                "equipmentReferences": b.equipment_references,
            }
            for b in self.sim.desk.bookings.values()
        ]
        return JSONResponse({"now": iso(self.sim.now), "bookings": bookings})

    async def control(self, request: Request) -> Response:
        action = request.path_params["action"]
        body = await _json(request) or {}
        sim = self.sim
        try:
            if action == "advance":
                sim.advance(hours=float(body.get("hours", 0)), days=float(body.get("days", 0)))
            elif action == "delay":
                sim.delay(body["voyage"], body["port"], hours=float(body["hours"]), reason=body.get("reason"))
            elif action == "override":
                sim.desk.set_override(body["reference"], body["action"], body.get("message"))
            elif action == "process":
                sim.desk.process(body.get("reference"))
            elif action == "mode":
                sim.desk.auto_process = bool(body["auto"])
            elif action == "complete":
                sim.desk.complete(body["reference"])
            else:
                return JSONResponse({"error": f"unknown control {action!r}"}, status_code=404)
        except (KeyError, ValueError, DeskError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        return JSONResponse({"now": iso(sim.now)})


def create_app(sim: Simulator) -> Starlette:
    api = CarrierApi(sim)
    return Starlette(
        routes=[
            Route("/bkg/v2/bookings", api.create_booking, methods=["POST"]),
            Route("/bkg/v2/bookings/{reference}", api.booking, methods=["GET", "PUT", "PATCH"]),
            Route("/cs/v1/point-to-point-routes", api.routes, methods=["GET"]),
            Route("/tnt/v3/events", api.events, methods=["GET"]),
            Route("/_sim/state", api.state, methods=["GET"]),
            Route("/_sim/{action}", api.control, methods=["POST"]),
        ],
    )
