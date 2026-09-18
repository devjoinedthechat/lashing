"""Track & Trace 3.0.0 events derived from the simulator's bookings, voyages and clock.

Events are computed, not stored: shipment events from each booking's history, transport events
from its legs (planned, estimated once a voyage is late, actual once the clock has passed), and
equipment events for each container. An event's ID is stable across calls, so a newer version of
the same event overrides the older one, as the standard intends.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from lashing.sim.desk import Desk, SimBooking, iso
from lashing.sim.world import CARRIER_CODE, PORTS, Leg

_NAMESPACE = uuid.UUID("5b1f0c4e-8d3a-4f7e-9c52-6a1d2e3f4b5c")
GATE_IN_BEFORE_DEPARTURE = dt.timedelta(hours=30)
GATE_OUT_AFTER_ARRIVAL = dt.timedelta(hours=24)

# Booking statuses and notes that map to a T&T shipment event type.
SHIPMENT_EVENT_TYPES = {
    "RECEIVED", "PENDING_UPDATE", "UPDATE_RECEIVED", "CONFIRMED", "PENDING_AMENDMENT", "REJECTED",
    "DECLINED", "CANCELLED", "COMPLETED", "AMENDMENT_RECEIVED", "AMENDMENT_CONFIRMED",
    "AMENDMENT_DECLINED", "AMENDMENT_CANCELLED", "CANCELLATION_RECEIVED", "CANCELLATION_CONFIRMED",
    "CANCELLATION_DECLINED",
}  # fmt: skip


def _event_id(*parts: object) -> str:
    return str(uuid.uuid5(_NAMESPACE, "/".join(str(p) for p in parts)))


def _routing() -> dict[str, Any]:
    return {"originatingParties": [{"partyCode": CARRIER_CODE, "codeListProvider": "ZZZ", "partyFunction": "CA"}]}


def _port_location(code: str) -> dict[str, str]:
    return {"UNLocationCode": code, "locationName": PORTS[code].name, "facilityType": "PORT_TERMINAL"}


def _transport_call(leg: Leg, index: int) -> dict[str, Any]:
    voyage = leg.voyage
    return {
        "transportCallReference": voyage.call_reference(index),
        "transportCallSequenceNumber": index + 1,
        "modeOfTransport": "VESSEL",
        "serviceCodeOrReference": {
            "carrierServiceCode": voyage.service.code,
            "universalServiceReference": voyage.service.universal_reference,
        },
        "exportVoyageNumberOrReference": {
            "carrierVoyageNumber": voyage.number,
            "universalVoyageReference": voyage.universal_reference,
        },
        "vesselTransport": {
            "vesselIMONumber": voyage.vessel.imo,
            "vesselName": voyage.vessel.name,
            "vesselFlag": voyage.vessel.flag,
            "operatorCarrierCode": CARRIER_CODE,
            "operatorCarrierCodeListProvider": "NMFTA",
        },
    }


class Tracker:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk

    @property
    def now(self) -> dt.datetime:
        return self.desk.clock.now

    def events(
        self,
        *,
        booking_reference: str | None = None,
        equipment_reference: str | None = None,
        event_types: set[str] | None = None,
        updated_min: dt.datetime | None = None,
        updated_max: dt.datetime | None = None,
    ) -> list[dict[str, Any]]:
        bookings = list(self.desk.bookings.values())
        if booking_reference is not None:
            bookings = [b for b in bookings if booking_reference in (b.booking_reference, b.request_reference)]
        if equipment_reference is not None:
            bookings = [b for b in bookings if equipment_reference in b.equipment_references]
        found: list[dict[str, Any]] = []
        for booking in bookings:
            found.extend(self._shipment_events(booking))
            found.extend(self._transport_events(booking))
            found.extend(self._equipment_events(booking, only=equipment_reference))
        if event_types:
            found = [e for e in found if e["eventClassification"]["eventType"] in event_types]
        if updated_min is not None:
            found = [e for e in found if dt.datetime.fromisoformat(e["eventUpdatedDateTime"]) >= updated_min]
        if updated_max is not None:
            found = [e for e in found if dt.datetime.fromisoformat(e["eventUpdatedDateTime"]) <= updated_max]
        # A stable sort by time alone: events at the same instant keep the order they happened in.
        # (Breaking ties by eventID, a hash, once listed PENDING_AMENDMENT before CONFIRMED.)
        return sorted(found, key=lambda e: e["eventDateTime"])

    def _document(self, booking: SimBooking) -> dict[str, str]:
        if booking.booking_reference:
            return {"type": "BOOKING", "reference": booking.booking_reference}
        return {"type": "CARRIER_BOOKING_REQUEST", "reference": booking.request_reference}

    def _shipment_events(self, booking: SimBooking) -> list[dict[str, Any]]:
        events = []
        for n, record in enumerate(booking.history):
            if record.status not in SHIPMENT_EVENT_TYPES:
                continue
            event: dict[str, Any] = {
                "eventID": _event_id(booking.request_reference, "shipment", n),
                "eventRouting": _routing(),
                "eventDateTime": iso(record.at),
                "eventUpdatedDateTime": iso(record.at),
                "eventClassification": {
                    "eventType": "SHIPMENT",
                    "eventClassifier": "ACTUAL",
                    "shipmentEventType": record.status,
                },
                "shipmentDetails": {"documentReference": self._document(booking)},
            }
            if record.reason:
                event["reason"] = record.reason
            events.append(event)
        return events

    def _milestones(
        self,
        planned: dt.datetime,
        estimated: dt.datetime,
        confirmed_at: dt.datetime,
        delay_known_at: dt.datetime | None,
    ) -> list[tuple[str, dt.datetime, dt.datetime]]:
        """The (classifier, event time, update time) a feed has published for one milestone by now.

        A feed accumulates: the planned event stays when an estimate or the actual follows it.
        """
        published = [("PLANNED", planned, confirmed_at)]
        if estimated != planned:
            published.append(("ESTIMATED", estimated, max(confirmed_at, delay_known_at or confirmed_at)))
        if self.now >= estimated:
            published.append(("ACTUAL", estimated, estimated))
        return published

    def _confirmed_at(self, booking: SimBooking) -> dt.datetime:
        times = [r.at for r in booking.history if r.status in ("CONFIRMED", "AMENDMENT_CONFIRMED")]
        return times[-1] if times else booking.submitted_at

    def _transport_events(self, booking: SimBooking) -> list[dict[str, Any]]:
        if booking.route is None:
            return []
        confirmed_at = self._confirmed_at(booking)
        events = []
        for leg in booking.route.legs:
            voyage = leg.voyage
            for index, kind in ((leg.load, "DEPARTED"), (leg.discharge, "ARRIVED")):
                call = voyage.calls[index]
                planned = call.planned_departure if kind == "DEPARTED" else call.planned_arrival
                estimated = voyage.estimated_departure(index) if kind == "DEPARTED" else voyage.estimated_arrival(index)
                published = self._milestones(planned, estimated, confirmed_at, voyage.delay_known_at)
                for classifier, when, updated in published:
                    event: dict[str, Any] = {
                        "eventID": _event_id(voyage.id, index, kind, classifier),
                        "eventRouting": _routing(),
                        "eventDateTime": iso(when),
                        "eventUpdatedDateTime": iso(min(updated, self.now)),
                        "eventLocation": _port_location(call.port),
                        "eventClassification": {
                            "eventType": "TRANSPORT",
                            "eventClassifier": classifier,
                            "transportEventType": kind,
                        },
                        "transportDetails": {"transportCall": _transport_call(leg, index)},
                        "shipmentDetails": {"documentReference": self._document(booking)},
                    }
                    if classifier != "PLANNED" and voyage.delay_at(index) and voyage.delay_reason:
                        event["reason"] = voyage.delay_reason
                    events.append(event)
        return events

    def _equipment_events(self, booking: SimBooking, only: str | None) -> list[dict[str, Any]]:
        if booking.route is None:
            return []
        legs = booking.route.legs
        milestones: list[tuple[str, Leg, int, dt.datetime, str, str]] = [
            ("GATED_IN", legs[0], legs[0].load, legs[0].departure - GATE_IN_BEFORE_DEPARTURE, "EXPORT", "LADEN"),
        ]
        for n, leg in enumerate(legs):
            phase_out = "EXPORT" if n == 0 else "TRANSSHIPMENT"
            phase_in = "IMPORT" if n == len(legs) - 1 else "TRANSSHIPMENT"
            milestones.append(("LOADED", leg, leg.load, leg.departure, phase_out, "LADEN"))
            milestones.append(("DISCHARGED", leg, leg.discharge, leg.arrival, phase_in, "LADEN"))
        milestones.append(
            ("GATED_OUT", legs[-1], legs[-1].discharge, legs[-1].arrival + GATE_OUT_AFTER_ARRIVAL, "IMPORT", "LADEN"),
        )
        events = []
        for container in booking.equipment_references:
            if only is not None and container != only:
                continue
            for kind, leg, index, when, phase, laden in milestones:
                if self.now < when:
                    continue  # equipment events are only ever reported as they happen
                code = booking.equipment_types.get(container, "22G1")
                event: dict[str, Any] = {
                    "eventID": _event_id(container, kind, leg.voyage.id, index),
                    "eventRouting": _routing(),
                    "eventDateTime": iso(when),
                    "eventUpdatedDateTime": iso(when),
                    "eventLocation": _port_location(leg.voyage.calls[index].port),
                    "eventClassification": {
                        "eventType": "EQUIPMENT",
                        "eventClassifier": "ACTUAL",
                        "equipmentEventType": kind,
                    },
                    "equipmentDetails": {
                        "equipmentReference": container,
                        "ISOEquipmentCode": code,
                        "emptyIndicator": laden,
                        "transportPhase": phase,
                    },
                    "shipmentDetails": {"documentReference": self._document(booking)},
                }
                if kind in ("LOADED", "DISCHARGED"):
                    event["transportDetails"] = {"transportCall": _transport_call(leg, index)}
                events.append(event)
        return events
