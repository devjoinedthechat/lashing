"""A small synthetic liner network: real port codes, fictional services, vessels and voyages.

Everything is generated deterministically from an epoch, so a scenario that says "the vessel on
LX1 leaving Shanghai next Monday is four days late" means the same thing on every run. The carrier,
services, vessels and IMO numbers are invented; only UN/LOCODEs and port names are real.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

CARRIER_CODE = "LSIM"  # the simulator's carrier, identified as an NMFTA SCAC-style code
CARRIER_CODE_LIST = "NMFTA"
CARRIER_NAME = "Lashing Simulated Lines"

MIN_CONNECTION = dt.timedelta(hours=24)
DOCUMENTATION_CUT_OFF = dt.timedelta(hours=48)
CARGO_CUT_OFF = dt.timedelta(hours=24)
DWELL = dt.timedelta(hours=20)


@dataclass(frozen=True)
class Port:
    code: str
    name: str
    utc_offset_minutes: int

    @property
    def zone(self) -> dt.timezone:
        return dt.timezone(dt.timedelta(minutes=self.utc_offset_minutes))


PORTS: dict[str, Port] = {
    p.code: p
    for p in (
        Port("CNSHA", "Shanghai", 480),
        Port("CNNGB", "Ningbo", 480),
        Port("KRPUS", "Busan", 540),
        Port("SGSIN", "Singapore", 480),
        Port("INNSA", "Nhava Sheva", 330),
        Port("AEJEA", "Jebel Ali", 240),
        Port("MAPTM", "Tanger Med", 60),
        Port("GBFXT", "Felixstowe", 60),
        Port("NLRTM", "Rotterdam", 120),
        Port("DEHAM", "Hamburg", 120),
        Port("BEANR", "Antwerp", 120),
        Port("DKAAR", "Aarhus", 120),
        Port("USLAX", "Los Angeles", -420),
    )
}


def imo_check_digit(first_six: str) -> int:
    return sum(int(d) * w for d, w in zip(first_six, range(7, 1, -1), strict=True)) % 10


def is_valid_imo(number: str) -> bool:
    return len(number) == 7 and number.isdigit() and imo_check_digit(number[:6]) == int(number[6])


@dataclass(frozen=True)
class Vessel:
    name: str
    imo: str
    flag: str = "DK"

    @classmethod
    def fictional(cls, name: str, serial: int) -> Vessel:
        first_six = f"98{serial:04d}"
        return cls(name=name, imo=f"{first_six}{imo_check_digit(first_six)}")


@dataclass(frozen=True)
class Service:
    code: str
    name: str
    universal_reference: str  # SR + 5 digits + letter
    direction: str  # N, E, W, S or R
    rotation: tuple[str, ...]
    arrival_days: tuple[int, ...]  # days after the voyage's first arrival, per call
    frequency_days: int
    first_offset_days: int  # first voyage's start relative to the epoch week
    vessels: tuple[Vessel, ...]
    capacity_teu: int = 2000


def _fleet(prefix: str, names: tuple[str, ...], first_serial: int) -> tuple[Vessel, ...]:
    return tuple(Vessel.fictional(f"{prefix} {name}", first_serial + i) for i, name in enumerate(names))


SERVICES: tuple[Service, ...] = (
    Service(
        code="LX1",
        name="Asia Europe Express 1",
        universal_reference="SR10001W",
        direction="W",
        rotation=("CNSHA", "CNNGB", "SGSIN", "NLRTM", "DEHAM", "BEANR"),
        arrival_days=(0, 2, 7, 30, 33, 35),
        frequency_days=7,
        first_offset_days=0,
        vessels=_fleet("LASHING", ("AURORA", "BOREAS", "CALYPSO", "DORADO", "ELARA", "FORNAX"), 1),
    ),
    Service(
        code="LX2",
        name="Asia Europe Express 2",
        universal_reference="SR10002W",
        direction="W",
        rotation=("KRPUS", "CNSHA", "SGSIN", "MAPTM", "GBFXT", "NLRTM"),
        arrival_days=(0, 2, 8, 27, 31, 33),
        frequency_days=7,
        first_offset_days=3,
        vessels=_fleet("LASHING", ("GEMINI", "HYDRA", "IRIS", "JUNO", "KESTREL"), 11),
    ),
    Service(
        code="LP1",
        name="Pacific Express",
        universal_reference="SR20001E",
        direction="E",
        rotation=("CNSHA", "KRPUS", "USLAX"),
        arrival_days=(0, 3, 15),
        frequency_days=7,
        first_offset_days=1,
        vessels=_fleet("LASHING", ("LYRA", "MIRA", "NOVA"), 21),
    ),
    Service(
        code="LA1",
        name="Asia Gulf Express",
        universal_reference="SR30001W",
        direction="W",
        rotation=("CNSHA", "SGSIN", "INNSA", "AEJEA"),
        arrival_days=(0, 6, 12, 16),
        frequency_days=7,
        first_offset_days=2,
        vessels=_fleet("LASHING", ("ORION", "PAVO", "RIGEL"), 31),
    ),
    Service(
        code="LF1",
        name="North Europe Feeder",
        universal_reference="SR40001N",
        direction="N",
        rotation=("NLRTM", "DKAAR"),
        arrival_days=(0, 2),
        frequency_days=3,
        first_offset_days=0,
        vessels=_fleet("LASHING FEEDER", ("SKAGEN", "TUNO"), 41),
        capacity_teu=600,
    ),
)


@dataclass
class Call:
    port: str
    sequence: int
    planned_arrival: dt.datetime
    planned_departure: dt.datetime


@dataclass
class Voyage:
    service: Service
    vessel: Vessel
    number: str  # carrier voyage number, e.g. 612W
    universal_reference: str  # e.g. 2612W
    calls: list[Call]
    capacity_teu: int
    booked_teu: int = 0
    delays: dict[int, dt.timedelta] = field(default_factory=dict)  # added at call index, carried forward
    delay_reason: str | None = None
    delay_known_at: dt.datetime | None = None

    def delay(self, port: str, by: dt.timedelta, reason: str | None, known_at: dt.datetime) -> None:
        """Delay the vessel from its call at `port` onwards; later calls inherit the delay."""
        index = self.index_of(port)
        self.delays[index] = self.delays.get(index, dt.timedelta()) + by
        self.delay_reason = reason
        self.delay_known_at = known_at

    @property
    def id(self) -> str:
        return f"{self.service.code}-{self.number}"

    def delay_at(self, index: int) -> dt.timedelta:
        return sum((d for i, d in self.delays.items() if i <= index), dt.timedelta())

    def estimated_arrival(self, index: int) -> dt.datetime:
        return self.calls[index].planned_arrival + self.delay_at(index)

    def estimated_departure(self, index: int) -> dt.datetime:
        return self.calls[index].planned_departure + self.delay_at(index)

    def index_of(self, port: str) -> int:
        for call in self.calls:
            if call.port == port:
                return call.sequence
        raise KeyError(f"{self.id} does not call at {port}")

    def call_reference(self, index: int) -> str:
        return f"{self.id}-{self.calls[index].port}"


@dataclass(frozen=True)
class Leg:
    voyage: Voyage
    load: int  # call index
    discharge: int

    @property
    def departure(self) -> dt.datetime:
        return self.voyage.estimated_departure(self.load)

    @property
    def arrival(self) -> dt.datetime:
        return self.voyage.estimated_arrival(self.discharge)

    @property
    def load_port(self) -> str:
        return self.voyage.calls[self.load].port

    @property
    def discharge_port(self) -> str:
        return self.voyage.calls[self.discharge].port


@dataclass(frozen=True)
class Route:
    legs: tuple[Leg, ...]

    @property
    def reference(self) -> str:
        return "LSIM:" + "|".join(f"{leg.voyage.id}:{leg.load}-{leg.discharge}" for leg in self.legs)

    @property
    def origin(self) -> str:
        return self.legs[0].load_port

    @property
    def destination(self) -> str:
        return self.legs[-1].discharge_port

    @property
    def departure(self) -> dt.datetime:
        return self.legs[0].departure

    @property
    def arrival(self) -> dt.datetime:
        return self.legs[-1].arrival

    @property
    def transit_days(self) -> int:
        return max(1, round((self.arrival - self.departure) / dt.timedelta(days=1)))

    def bookable(self, now: dt.datetime) -> bool:
        """Still bookable: its earliest cut-off (documentation, 48 hours before sailing) has not passed."""
        return min(self.cut_offs().values()) > now

    def cut_offs(self) -> dict[str, dt.datetime]:
        """DCO documentation, FCO full-container delivery, VCO verified gross mass: before the first departure."""
        first = self.legs[0].voyage.calls[self.legs[0].load].planned_departure
        return {"DCO": first - DOCUMENTATION_CUT_OFF, "FCO": first - CARGO_CUT_OFF, "VCO": first - CARGO_CUT_OFF}


class World:
    def __init__(self, epoch: dt.datetime, weeks_before: int = 3, weeks_after: int = 12) -> None:
        if epoch.tzinfo is None:
            raise ValueError("epoch must be timezone-aware")
        self.epoch = epoch
        self.voyages: dict[str, Voyage] = {}
        start = (epoch - dt.timedelta(days=epoch.weekday())).replace(hour=6, minute=0, second=0, microsecond=0)
        for service in SERVICES:
            first = start + dt.timedelta(days=service.first_offset_days - 7 * weeks_before)
            count = (7 * (weeks_before + weeks_after)) // service.frequency_days
            for n in range(count):
                self._add_voyage(service, n, first + dt.timedelta(days=n * service.frequency_days))

    def _add_voyage(self, service: Service, n: int, first_arrival: dt.datetime) -> None:
        calls = [
            Call(
                port=port,
                sequence=i,
                planned_arrival=first_arrival + dt.timedelta(days=days),
                planned_departure=first_arrival + dt.timedelta(days=days) + DWELL,
            )
            for i, (port, days) in enumerate(zip(service.rotation, service.arrival_days, strict=True))
        ]
        sequence = 600 + n
        voyage = Voyage(
            service=service,
            vessel=service.vessels[n % len(service.vessels)],
            number=f"{sequence}{service.direction}",
            universal_reference=f"{first_arrival.year % 100:02d}{sequence % 100:02d}{service.direction}",
            calls=calls,
            capacity_teu=service.capacity_teu,
        )
        self.voyages[voyage.id] = voyage

    def voyage(self, voyage_id: str) -> Voyage:
        try:
            return self.voyages[voyage_id]
        except KeyError:
            raise KeyError(f"no voyage {voyage_id}") from None

    def find_voyage(self, voyage_number: str, vessel_name: str | None = None) -> Voyage | None:
        for voyage in self.voyages.values():
            if voyage.number == voyage_number and (vessel_name is None or voyage.vessel.name == vessel_name.upper()):
                return voyage
        return None

    def route(self, reference: str) -> Route:
        if not reference.startswith("LSIM:"):
            raise KeyError(f"unknown routing reference {reference!r}")
        legs: list[Leg] = []
        for part in reference.removeprefix("LSIM:").split("|"):
            voyage_id, span = part.rsplit(":", 1)
            load, discharge = (int(x) for x in span.split("-"))
            voyage = self.voyage(voyage_id)
            if not 0 <= load < discharge < len(voyage.calls):
                raise KeyError(f"routing reference {reference!r} does not match {voyage_id}")
            legs.append(Leg(voyage, load, discharge))
        return Route(tuple(legs))

    def _legs_from(self, origin: str, earliest: dt.datetime, latest: dt.datetime) -> list[Leg]:
        legs: list[Leg] = []
        for voyage in self.voyages.values():
            for call in voyage.calls:
                if call.port != origin:
                    continue
                departure = voyage.estimated_departure(call.sequence)
                if earliest <= departure <= latest:
                    legs.extend(Leg(voyage, call.sequence, j) for j in range(call.sequence + 1, len(voyage.calls)))
        return legs

    @staticmethod
    def _revisits(leg: Leg, port: str) -> bool:
        return any(call.port == port for call in leg.voyage.calls[leg.load : leg.discharge + 1])

    def routes(
        self,
        origin: str,
        destination: str,
        earliest: dt.datetime,
        latest: dt.datetime,
        max_transshipments: int = 1,
    ) -> list[Route]:
        """Sailings from origin to destination departing within the window, direct or with one transshipment."""
        found: list[Route] = []
        for leg in self._legs_from(origin, earliest, latest):
            if leg.discharge_port == destination:
                found.append(Route((leg,)))
            elif max_transshipments >= 1 and leg.discharge_port not in (origin, destination):
                window_end = latest + dt.timedelta(days=60)
                onward = [
                    nxt
                    for nxt in self._legs_from(leg.discharge_port, leg.arrival + MIN_CONNECTION, window_end)
                    if nxt.discharge_port == destination and not self._revisits(nxt, origin)
                ]
                if onward:
                    found.append(Route((leg, min(onward, key=lambda n: n.arrival))))
        direct_ports = {r.legs[0].voyage.id for r in found if len(r.legs) == 1}
        # A transshipment on a voyage that also goes direct is never the better offer.
        found = [r for r in found if len(r.legs) == 1 or r.legs[0].voyage.id not in direct_ports]
        return sorted(found, key=lambda r: (r.arrival, len(r.legs), r.departure))
