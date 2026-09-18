"""Commercial Schedules 1.0.4 point-to-point routes from the simulator's network."""

from __future__ import annotations

import datetime as dt
from typing import Any

from lashing.sim.world import CARRIER_CODE, CARRIER_CODE_LIST, PORTS, Leg, Route


def local(moment: dt.datetime, port: str) -> str:
    """CS dateTimes are local to the place, with its UTC offset."""
    return moment.astimezone(PORTS[port].zone).isoformat(timespec="seconds")


def _place(port: str, moment: dt.datetime, call_reference: str | None = None) -> dict[str, Any]:
    place: dict[str, Any] = {
        "facilityTypeCode": "POTE",
        "location": {"UNLocationCode": port, "locationName": PORTS[port].name},
        "dateTime": local(moment, port),
    }
    if call_reference:
        place["transportCallReference"] = call_reference
    return place


def _leg(sequence: int, leg: Leg) -> dict[str, Any]:
    voyage = leg.voyage
    return {
        "sequenceNumber": sequence,
        "transport": {
            "modeOfTransport": "VESSEL",
            "servicePartners": [
                {
                    "carrierCode": CARRIER_CODE,
                    "carrierCodeListProvider": CARRIER_CODE_LIST,
                    "carrierServiceCode": voyage.service.code,
                    "carrierServiceName": voyage.service.name,
                    "carrierExportVoyageNumber": voyage.number,
                },
            ],
            "universalServiceReference": voyage.service.universal_reference,
            "universalExportVoyageReference": voyage.universal_reference,
            "vessel": {
                "vesselIMONumber": voyage.vessel.imo,
                "name": voyage.vessel.name,
                "flag": voyage.vessel.flag,
                "operatorCarrierCode": CARRIER_CODE,
                "operatorCarrierCodeListProvider": CARRIER_CODE_LIST,
            },
        },
        "departure": _place(leg.load_port, leg.departure, voyage.call_reference(leg.load)),
        "arrival": _place(leg.discharge_port, leg.arrival, voyage.call_reference(leg.discharge)),
    }


def point_to_point(route: Route, solution_number: int) -> dict[str, Any]:
    return {
        "placeOfReceipt": _place(route.origin, route.departure),
        "placeOfDelivery": _place(route.destination, route.arrival),
        "receiptTypeAtOrigin": "CY",
        "deliveryTypeAtDestination": "CY",
        "cutOffTimes": [
            {"cutOffDateTimeCode": code, "cutOffDateTime": local(moment, route.origin)}
            for code, moment in route.cut_offs().items()
        ],
        "solutionNumber": solution_number,
        "routingReference": route.reference,
        "transitTime": route.transit_days,
        "legs": [_leg(i, leg) for i, leg in enumerate(route.legs, start=1)],
    }
