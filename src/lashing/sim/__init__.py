"""A simulated DCSA carrier, so lashing can be run, tested and evaluated with no carrier access."""

from __future__ import annotations

import datetime as dt
from typing import Any

from lashing.sim.desk import Clock, Desk
from lashing.sim.schedules import point_to_point
from lashing.sim.tracking import Tracker
from lashing.sim.world import World

DEFAULT_START = dt.datetime(2026, 9, 21, 8, 0, tzinfo=dt.UTC)


class Simulator:
    """One carrier: a network of voyages, a booking desk, a tracking feed and a clock they share."""

    def __init__(self, start: dt.datetime = DEFAULT_START) -> None:
        self.clock = Clock(start)
        self.world = World(start)
        self.desk = Desk(self.world, self.clock)
        self.tracker = Tracker(self.desk)

    @property
    def now(self) -> dt.datetime:
        return self.clock.now

    def advance(self, *, hours: float = 0, days: float = 0) -> dt.datetime:
        now = self.clock.advance(dt.timedelta(hours=hours, days=days))
        self.desk.process()
        return now

    def delay(self, voyage_id: str, port: str, *, hours: float, reason: str | None = None) -> None:
        self.world.voyage(voyage_id).delay(port, dt.timedelta(hours=hours), reason, self.clock.now)

    def point_to_point(
        self,
        origin: str,
        destination: str,
        earliest: dt.datetime | None = None,
        latest: dt.datetime | None = None,
        max_transshipments: int = 1,
    ) -> list[dict[str, Any]]:
        earliest = earliest or self.clock.now
        latest = latest or earliest + dt.timedelta(days=21)
        routes = self.world.routes(origin, destination, earliest, latest, max_transshipments)
        bookable = [r for r in routes if r.bookable(self.clock.now)]
        return [point_to_point(route, n) for n, route in enumerate(bookable, start=1)]


__all__ = ["DEFAULT_START", "Simulator"]
