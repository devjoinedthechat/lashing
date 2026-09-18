"""The simulated carrier's booking desk: the provider side of DCSA Booking 2.0.

It keeps the state machine the standard describes, applies a carrier's ordinary rules (known
equipment, cargo weight present, a sailing with space whose cut-off has not passed), and exposes
scenario hooks so tests and evals can make the carrier ask for updates, reject, decline or say
anything at all in its feedback, including text written to manipulate an agent.
"""

from __future__ import annotations

import copy
import datetime as dt
import itertools
from dataclasses import dataclass, field
from typing import Any

from lashing.dcsa.booking import (
    AFTER_CONFIRMATION,
    BEFORE_CONFIRMATION,
    TERMINAL,
    AmendmentStatus,
    BookingStatus,
    Cancellation,
    CancellationStatus,
    LifecycleError,
    cancellation_kind,
)
from lashing.sim.world import CARRIER_CODE, CARRIER_CODE_LIST, PORTS, Route, World

# ISO 6346 size-type codes and their size-type groups, with the TEU each one takes.
EQUIPMENT_TEU: dict[str, int] = {
    "22G1": 1, "22GP": 1, "22R1": 1, "22RT": 1,
    "42G1": 2, "42GP": 2, "42R1": 2, "42RT": 2,
    "45G1": 2, "45GP": 2, "45R1": 2, "45RT": 2,
}  # fmt: skip

SEARCH_WINDOW = dt.timedelta(days=21)


class DeskError(Exception):
    def __init__(self, status: int, message: str, *, json_path: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.json_path = json_path


class _Refused(Exception):
    """The carrier cannot plan a request; carries the feedback it gives and whether it is final."""

    def __init__(self, message: str, json_path: str, *, final: bool = False) -> None:
        super().__init__(message)
        self.feedback = feedback("ERROR", "PROPERTY_VALUE_MUST_CHANGE", message, json_path)
        self.final = final


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("the clock needs a timezone-aware time")
        self.now = now

    def advance(self, delta: dt.timedelta) -> dt.datetime:
        if delta < dt.timedelta():
            raise ValueError("the clock only moves forward")
        self.now += delta
        return self.now


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def feedback(severity: str, code: str, message: str, json_path: str | None = None) -> dict[str, str]:
    item = {"severity": severity, "code": code, "message": message}
    if json_path:
        item["jsonPath"] = json_path
    return item


@dataclass
class Override:
    """What the carrier does next with one booking, set by a scenario."""

    action: str  # hold | request_update | reject | request_amendment | decline | decline_amendment
    message: str | None = None


@dataclass
class Record:
    """One status change, kept for Track & Trace shipment events."""

    at: dt.datetime
    status: str
    reason: str | None = None


@dataclass
class SimBooking:
    request_reference: str
    request: dict[str, Any]
    submitted_at: dt.datetime
    status: BookingStatus = BookingStatus.RECEIVED
    booking_reference: str | None = None
    amendment: AmendmentStatus | None = None
    cancellation: CancellationStatus | None = None
    amended_request: dict[str, Any] | None = None
    route: Route | None = None
    teu: int = 0
    feedbacks: list[dict[str, str]] = field(default_factory=list)
    equipment_references: list[str] = field(default_factory=list)
    equipment_types: dict[str, str] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)  # request | amendment | cancellation
    history: list[Record] = field(default_factory=list)


def container_number(serial: int, owner: str = "LSM") -> str:
    """An ISO 6346 container number with a valid check digit (owner code + U + 6 digits + check)."""
    letters = {}
    value = 10
    for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if value % 11 == 0:
            value += 1
        letters[char] = value
        value += 1
    body = f"{owner}U{serial:06d}"
    total = sum((letters[c] if c.isalpha() else int(c)) * 2**i for i, c in enumerate(body))
    return f"{body}{total % 11 % 10}"


def _locations(request: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in request.get("shipmentLocations", []):
        code = item.get("location", {}).get("UNLocationCode")
        if code:
            found.setdefault(item["locationTypeCode"], code)
    return found


class Desk:
    def __init__(self, world: World, clock: Clock) -> None:
        self.world = world
        self.clock = clock
        self.bookings: dict[str, SimBooking] = {}
        self._by_booking_reference: dict[str, str] = {}
        self.overrides: dict[str, Override] = {}
        self.auto_process = True
        self._request_serial = itertools.count(1)
        self._booking_serial = itertools.count(1)
        self._container_serial = itertools.count(100001)

    # -- lookup --------------------------------------------------------------------------------

    def find(self, reference: str) -> SimBooking:
        key = self._by_booking_reference.get(reference, reference)
        try:
            return self.bookings[key]
        except KeyError:
            raise DeskError(404, f"no booking with reference {reference!r}") from None

    def _override(self, booking: SimBooking) -> Override | None:
        for reference in (booking.booking_reference, booking.request_reference):
            if reference and reference in self.overrides:
                return self.overrides[reference]
        return None

    def _set(self, booking: SimBooking, status: BookingStatus, reason: str | None = None) -> None:
        booking.status = status
        booking.history.append(Record(self.clock.now, status.value, reason))

    def _note(self, booking: SimBooking, event: str, reason: str | None = None) -> None:
        booking.history.append(Record(self.clock.now, event, reason))

    # -- shipper operations (POST, GET, PUT, PATCH) --------------------------------------------

    def submit(self, request: dict[str, Any]) -> str:
        reference = f"cbrr-{next(self._request_serial):05d}"
        booking = SimBooking(reference, copy.deepcopy(request), self.clock.now, pending={"request"})
        self.bookings[reference] = booking
        self._note(booking, BookingStatus.RECEIVED.value)
        return reference

    def view(self, reference: str, *, amended: bool = False) -> dict[str, Any]:
        booking = self.find(reference)
        if amended:
            if booking.amended_request is None or booking.status is BookingStatus.PENDING_AMENDMENT:
                raise DeskError(404, f"booking {reference!r} has no amendment to show")
            return self._payload(booking, booking.amended_request)
        return self._payload(booking, booking.request)

    def change(self, reference: str, request: dict[str, Any]) -> None:
        booking = self.find(reference)
        if booking.status in BEFORE_CONFIRMATION:
            if reference not in (booking.request_reference, booking.booking_reference):
                raise DeskError(404, f"no booking with reference {reference!r}")
            booking.request = self._strip_references(request)
            booking.feedbacks = []
            self._set(booking, BookingStatus.UPDATE_RECEIVED)
            booking.pending.add("request")
            return
        if booking.status in AFTER_CONFIRMATION:
            if booking.cancellation is CancellationStatus.CANCELLATION_RECEIVED:
                raise DeskError(409, "a cancellation of this booking is awaiting processing")
            # Booking 2.0: the PUT path "can contain one of carrierBookingRequestReference or carrierBookingReference".
            booking.amended_request = self._strip_references(request)
            booking.amendment = AmendmentStatus.AMENDMENT_RECEIVED
            if booking.status is BookingStatus.PENDING_AMENDMENT:
                booking.status = BookingStatus.CONFIRMED
            self._note(booking, AmendmentStatus.AMENDMENT_RECEIVED.value)
            booking.pending.add("amendment")
            return
        raise DeskError(409, f"booking {reference!r} cannot be changed in status {booking.status.value}")

    def cancel(self, reference: str, body: dict[str, Any]) -> None:
        booking = self.find(reference)
        try:
            kind = cancellation_kind(body)
        except LifecycleError as error:
            raise DeskError(400, str(error)) from None
        reason = body.get("reason")
        # The CancelBookingRequest descriptions say a confirmed booking or its amendment is addressed by
        # carrierBookingReference, but DCSA's Conformance Framework sends the request reference when it
        # has had no notification carrying the booking reference. Either reference names one booking,
        # so the simulator accepts both; lashing's own client always sends the one the text asks for.
        if kind is Cancellation.REQUEST:
            if booking.status not in BEFORE_CONFIRMATION:
                raise DeskError(409, f"a booking in status {booking.status.value} cannot be cancelled this way")
            # UseCase 11 has no carrier step: a request the carrier has not confirmed is cancelled on receipt.
            booking.pending.clear()
            self._release(booking)
            self._set(booking, BookingStatus.CANCELLED, reason)
        elif kind is Cancellation.AMENDMENT:
            if booking.amendment is not AmendmentStatus.AMENDMENT_RECEIVED:
                raise DeskError(404, "there is no pending amendment to cancel")
            booking.pending.discard("amendment")
            booking.amendment = AmendmentStatus.AMENDMENT_CANCELLED
            booking.amended_request = None
            self._note(booking, AmendmentStatus.AMENDMENT_CANCELLED.value, reason)
        else:
            if booking.status not in AFTER_CONFIRMATION:
                raise DeskError(409, f"a booking in status {booking.status.value} is not confirmed")
            if booking.cancellation is CancellationStatus.CANCELLATION_RECEIVED:
                raise DeskError(409, "a cancellation of this booking is already awaiting processing")
            booking.cancellation = CancellationStatus.CANCELLATION_RECEIVED
            self._note(booking, CancellationStatus.CANCELLATION_RECEIVED.value, reason)
            booking.pending.add("cancellation")

    @staticmethod
    def _strip_references(request: dict[str, Any]) -> dict[str, Any]:
        content = copy.deepcopy(request)
        content.pop("carrierBookingRequestReference", None)
        content.pop("carrierBookingReference", None)
        return content

    # -- the carrier's side ----------------------------------------------------------------------

    def process(self, reference: str | None = None) -> None:
        """Handle everything waiting for the carrier (or for one booking), as a desk would between two polls."""
        targets = [self.find(reference)] if reference else list(self.bookings.values())
        for booking in targets:
            override = self._override(booking)
            if override is not None and override.action == "hold":
                continue
            if "request" in booking.pending:
                booking.pending.discard("request")
                self._decide_request(booking, override)
            if "amendment" in booking.pending:
                booking.pending.discard("amendment")
                self._decide_amendment(booking, override)
            if "cancellation" in booking.pending:
                booking.pending.discard("cancellation")
                self._decide_cancellation(booking, override)
            if override is not None and override.action in ("request_amendment", "decline"):
                self._carrier_initiated(booking, override)

    def _decide_request(self, booking: SimBooking, override: Override | None) -> None:
        if override is not None and override.action == "request_update":
            self.overrides.pop(booking.request_reference, None)
            message = override.message or "Please review and resubmit the booking."
            booking.feedbacks = [feedback("ERROR", "PROPERTY_VALUE_MUST_CHANGE", message)]
            self._set(booking, BookingStatus.PENDING_UPDATE, message)
            return
        if override is not None and override.action == "reject":
            message = override.message or "The booking request was rejected."
            booking.feedbacks = [feedback("ERROR", "INFORMATIONAL_MESSAGE", message)]
            self._set(booking, BookingStatus.REJECTED, message)
            return
        problems = self._problems(booking.request)
        route, final = None, False
        try:
            route = self._plan(booking.request, booking)
        except _Refused as refusal:
            problems.append(refusal.feedback)
            final = refusal.final
        if problems or route is None:
            booking.feedbacks = problems
            status = BookingStatus.REJECTED if final else BookingStatus.PENDING_UPDATE
            self._set(booking, status, problems[0]["message"])
            return
        self._allocate(booking, route, booking.request)
        if booking.booking_reference is None:
            booking.booking_reference = f"LSIM{next(self._booking_serial):06d}"
            self._by_booking_reference[booking.booking_reference] = booking.request_reference
        self._assign_equipment(booking)
        booking.feedbacks = []
        self._set(booking, BookingStatus.CONFIRMED)

    def _decide_amendment(self, booking: SimBooking, override: Override | None) -> None:
        amended = booking.amended_request
        if amended is None:
            return
        problems = self._problems(amended)
        route = None
        try:
            route = self._plan(amended, booking)
        except _Refused as refusal:
            problems.append(refusal.feedback)
        if override is not None and override.action == "decline_amendment":
            self.overrides.pop(booking.booking_reference or "", None)
            problems.append(feedback("ERROR", "INFORMATIONAL_MESSAGE", override.message or "Amendment declined."))
        if problems or route is None:
            booking.feedbacks = problems
            booking.amendment = AmendmentStatus.AMENDMENT_DECLINED
            booking.amended_request = None
            self._note(booking, AmendmentStatus.AMENDMENT_DECLINED.value, problems[0]["message"])
            return
        self._release(booking)
        booking.request = amended
        booking.amended_request = None
        self._allocate(booking, route, booking.request)
        self._assign_equipment(booking)
        booking.feedbacks = []
        booking.amendment = AmendmentStatus.AMENDMENT_CONFIRMED
        self._note(booking, AmendmentStatus.AMENDMENT_CONFIRMED.value)
        self._set(booking, BookingStatus.CONFIRMED)

    def _decide_cancellation(self, booking: SimBooking, override: Override | None = None) -> None:
        sailed = booking.route is not None and self.clock.now >= booking.route.departure
        if sailed or (override is not None and override.action == "decline_cancellation"):
            for reference in (booking.booking_reference, booking.request_reference):
                self.overrides.pop(reference or "", None)
            message = (
                "The cargo has already sailed; the booking can no longer be cancelled."
                if sailed
                else (override.message if override and override.message else "The cancellation was declined.")
            )
            booking.cancellation = CancellationStatus.CANCELLATION_DECLINED
            booking.feedbacks = [feedback("ERROR", "INFORMATIONAL_MESSAGE", message)]
            self._note(booking, CancellationStatus.CANCELLATION_DECLINED.value, message)
            return
        self._release(booking)
        booking.cancellation = CancellationStatus.CANCELLATION_CONFIRMED
        self._note(booking, CancellationStatus.CANCELLATION_CONFIRMED.value)
        self._set(booking, BookingStatus.CANCELLED)

    def _carrier_initiated(self, booking: SimBooking, override: Override) -> None:
        if booking.status is not BookingStatus.CONFIRMED:
            return
        for reference in (booking.booking_reference, booking.request_reference):
            self.overrides.pop(reference or "", None)
        message = override.message or "The carrier needs changes to this booking."
        booking.feedbacks = [feedback("ERROR", "PROPERTY_VALUE_MUST_CHANGE", message)]
        if override.action == "request_amendment":
            self._set(booking, BookingStatus.PENDING_AMENDMENT, message)
        else:
            self._release(booking)
            self._set(booking, BookingStatus.DECLINED, message)

    # -- carrier rules ---------------------------------------------------------------------------

    def _problems(self, request: dict[str, Any]) -> list[dict[str, str]]:
        problems = []
        substitution = bool(request.get("isEquipmentSubstitutionAllowed"))
        for i, equipment in enumerate(request.get("requestedEquipments", [])):
            path = f"$.requestedEquipments[{i}]"
            code = equipment.get("ISOEquipmentCode")
            if code not in EQUIPMENT_TEU and not substitution:
                problems.append(
                    feedback(
                        "ERROR",
                        "PROPERTY_VALUE_MUST_CHANGE",
                        f"Equipment {code} is not offered. Offered: {', '.join(sorted(EQUIPMENT_TEU))}.",
                        f"{path}.ISOEquipmentCode",
                    ),
                )
            commodities = equipment.get("commodities", [])
            weighed = "cargoGrossWeight" in equipment or (
                commodities and all("cargoGrossWeight" in c for c in commodities)
            )
            if not weighed:
                problems.append(
                    feedback(
                        "ERROR",
                        "PROPERTY_VALUE_MUST_CHANGE",
                        "Cargo gross weight is required for every requested equipment.",
                        f"{path}.cargoGrossWeight",
                    ),
                )
        return problems

    def _teu(self, request: dict[str, Any]) -> int:
        return sum(
            EQUIPMENT_TEU.get(e.get("ISOEquipmentCode", ""), 2) * int(e.get("units", 0))
            for e in request.get("requestedEquipments", [])
        )

    def _plan(self, request: dict[str, Any], booking: SimBooking) -> Route:
        """The sailing for a request; raises _Refused with the carrier's feedback when there is none."""
        places = _locations(request)
        origin = places.get("POL") or places.get("PRE")
        destination = places.get("POD") or places.get("PDE")
        if reference := request.get("routingReference"):
            route = self._by_routing_reference(reference)
        elif (voyage_number := request.get("carrierExportVoyageNumber")) and origin and destination:
            route = self._by_voyage(voyage_number, request.get("vessel", {}).get("name"), origin, destination)
        else:
            route = self._by_search(origin, destination, request.get("expectedDepartureDate"))
        if route.cut_offs()["FCO"] <= self.clock.now:
            voyage_id = route.legs[0].voyage.id
            raise _Refused(
                f"The cargo cut-off for {voyage_id} has passed; choose a later sailing.", "$.routingReference"
            )
        teu = self._teu(request)
        held = {leg.voyage.id for leg in booking.route.legs} if booking.route else set()
        for leg in route.legs:
            voyage = leg.voyage
            already = booking.teu if voyage.id in held else 0
            if voyage.booked_teu - already + teu > voyage.capacity_teu:
                message = f"{voyage.vessel.name} voyage {voyage.number} is fully booked; choose another sailing."
                raise _Refused(message, "$.routingReference")
        return route

    def _by_routing_reference(self, reference: str) -> Route:
        try:
            return self.world.route(reference)
        except KeyError:
            raise _Refused("Unknown routingReference.", "$.routingReference", final=True) from None

    def _by_voyage(self, voyage_number: str, vessel: str | None, origin: str, destination: str) -> Route:
        path = "$.carrierExportVoyageNumber"
        voyage = self.world.find_voyage(voyage_number, vessel)
        if voyage is None:
            raise _Refused(f"No voyage {voyage_number}" + (f" on {vessel}" if vessel else "") + ".", path)
        try:
            load, discharge = voyage.index_of(origin), voyage.index_of(destination)
        except KeyError as error:
            raise _Refused(str(error).strip("'") + ".", path) from None
        if load >= discharge:
            raise _Refused(f"{voyage.id} does not sail from {origin} to {destination}.", path)
        return self.world.route(f"LSIM:{voyage.id}:{load}-{discharge}")

    def _by_search(self, origin: str | None, destination: str | None, requested: str | None) -> Route:
        if not origin or not destination:
            message = "A port of loading and a port of discharge are required."
            raise _Refused(message, "$.shipmentLocations", final=True)
        for code, where in ((origin, "loading"), (destination, "discharge")):
            if code not in PORTS:
                raise _Refused(f"{code} is not served as a port of {where}.", "$.shipmentLocations", final=True)
        earliest = self.clock.now
        if requested:
            earliest = max(earliest, dt.datetime.fromisoformat(requested).replace(tzinfo=dt.UTC))
        candidates = [
            r
            for r in self.world.routes(origin, destination, earliest, earliest + SEARCH_WINDOW)
            if r.bookable(self.clock.now)
        ]
        if not candidates:
            message = f"No sailing from {origin} to {destination} within three weeks of the requested date."
            raise _Refused(message, "$.expectedDepartureDate")
        return candidates[0]  # earliest arrival, then fewest legs: what a shipper would pick

    def _allocate(self, booking: SimBooking, route: Route, request: dict[str, Any]) -> None:
        self._release(booking)
        booking.route = route
        booking.teu = self._teu(request)
        for leg in route.legs:
            leg.voyage.booked_teu += booking.teu

    def _release(self, booking: SimBooking) -> None:
        if booking.route is not None:
            for leg in booking.route.legs:
                leg.voyage.booked_teu -= booking.teu
        booking.route = None
        booking.teu = 0

    def _assign_equipment(self, booking: SimBooking) -> None:
        """One container per requested unit, keeping the numbers already given out where the type still fits."""
        wanted = [
            e.get("ISOEquipmentCode", "22G1")
            for e in booking.request.get("requestedEquipments", [])
            for _ in range(int(e.get("units", 0)))
        ]
        spare = list(booking.equipment_references)
        assigned: list[str] = []
        types: dict[str, str] = {}
        for code in wanted:
            match = next((c for c in spare if booking.equipment_types.get(c) == code), None)
            if match is None:
                match = container_number(next(self._container_serial))
            else:
                spare.remove(match)
            assigned.append(match)
            types[match] = code
        booking.equipment_references = assigned
        booking.equipment_types = types

    # -- payloads --------------------------------------------------------------------------------

    def _payload(self, booking: SimBooking, content: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"carrierBookingRequestReference": booking.request_reference}
        if booking.booking_reference:
            payload["carrierBookingReference"] = booking.booking_reference
        payload["bookingStatus"] = booking.status.value
        if booking.amendment:
            payload["amendedBookingStatus"] = booking.amendment.value
        if booking.cancellation:
            payload["bookingCancellationStatus"] = booking.cancellation.value
        payload.update(copy.deepcopy(content))
        if booking.booking_reference:
            self._reference_commodities(payload, booking.booking_reference)
        if booking.feedbacks:
            payload["feedbacks"] = copy.deepcopy(booking.feedbacks)
        if booking.route is not None:  # a confirmed booking (and its pending amendment) carries the plan
            payload["confirmedEquipments"] = [  # what the carrier confirmed, not what an amendment asks for
                {"ISOEquipmentCode": e["ISOEquipmentCode"], "units": e["units"]}
                for e in booking.request.get("requestedEquipments", [])
            ]
            payload["transportPlan"] = [self._transport(i, leg) for i, leg in enumerate(booking.route.legs, start=1)]
            payload["shipmentCutOffTimes"] = [
                {"cutOffDateTimeCode": code, "cutOffDateTime": iso(moment)}
                for code, moment in booking.route.cut_offs().items()
            ]
        return payload

    @staticmethod
    def _reference_commodities(payload: dict[str, Any], booking_reference: str) -> None:
        """A confirmed booking gives every commodity a carrier reference the shipping instructions will quote."""
        for i, equipment in enumerate(payload.get("requestedEquipments", []), start=1):
            for j, commodity in enumerate(equipment.get("commodities", []), start=1):
                commodity.setdefault("commoditySubReference", f"{booking_reference}-{i}-{j}")

    @staticmethod
    def _transport(sequence: int, leg: Any) -> dict[str, Any]:
        voyage = leg.voyage
        return {
            "transportPlanStage": "MNC",
            "transportPlanStageSequenceNumber": sequence,
            "loadLocation": {"UNLocationCode": leg.load_port, "locationName": PORTS[leg.load_port].name},
            "dischargeLocation": {"UNLocationCode": leg.discharge_port, "locationName": PORTS[leg.discharge_port].name},
            "plannedDepartureDate": voyage.calls[leg.load].planned_departure.date().isoformat(),
            "plannedArrivalDate": voyage.calls[leg.discharge].planned_arrival.date().isoformat(),
            "modeOfTransport": "VESSEL",
            "vesselName": voyage.vessel.name,
            "vesselIMONumber": voyage.vessel.imo,
            "carrierServiceCode": voyage.service.code,
            "universalServiceReference": voyage.service.universal_reference,
            "carrierExportVoyageNumber": voyage.number,
            "universalExportVoyageReference": voyage.universal_reference,
        }

    # -- scenario hooks --------------------------------------------------------------------------

    def set_override(self, reference: str, action: str, message: str | None = None) -> None:
        allowed = {
            "hold", "request_update", "reject", "request_amendment", "decline", "decline_amendment",
            "decline_cancellation",
        }  # fmt: skip
        if action not in allowed:
            raise ValueError(f"unknown override {action!r}; use one of {sorted(allowed)}")
        self.overrides[reference] = Override(action, message)

    def clear_override(self, reference: str) -> None:
        self.overrides.pop(reference, None)

    def complete(self, reference: str) -> None:
        booking = self.find(reference)
        if booking.status in TERMINAL:
            raise DeskError(409, f"booking {reference!r} is already {booking.status.value}")
        self._set(booking, BookingStatus.COMPLETED)

    @property
    def carrier(self) -> dict[str, str]:
        return {"carrierCode": CARRIER_CODE, "carrierCodeListProvider": CARRIER_CODE_LIST}
