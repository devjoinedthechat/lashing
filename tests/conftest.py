from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp_types import ElicitResult

from lashing.config import DEMO_SHIPPER, Approvals, Config, Grant
from lashing.server import build_server, demo
from lashing.service import Lashing
from lashing.sim import Simulator

FIXTURES = Path(__file__).parent / "fixtures" / "dcsa"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sim() -> Simulator:
    return Simulator()


Factory = Callable[..., Lashing]


@pytest.fixture
def make_lashing(tmp_path: Path, sim: Simulator) -> Factory:
    """A lashing service over the simulator, with the given grants and approval channels."""

    def make(*grants: Grant, approvals: Approvals | None = None, state_dir: Path | None = None) -> Lashing:
        config = Config(
            endpoints=None,
            shipper=DEMO_SHIPPER,
            grants=grants,
            approvals=approvals or Approvals(),
            state_dir=state_dir or tmp_path / "state",
        )
        service, _ = demo(config.state_dir, sim=sim, config=config)
        return service

    return make


def grant(*actions: str, **fields: Any) -> Grant:
    return Grant(id=fields.pop("id", "test-grant"), actions=frozenset(actions), **fields)


class Person:
    """The human behind the client's approval prompt, scripted."""

    def __init__(self, answer: str = "approve") -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.asked.append(params.message)
        if self.answer == "approve":
            return ElicitResult(action="accept", content={"approve": True})
        if self.answer == "say_no":
            return ElicitResult(action="accept", content={"approve": False})
        if self.answer == "decline":
            return ElicitResult(action="decline")
        return ElicitResult(action="cancel")


class ToolFailed(Exception):
    pass


class Tools:
    """An agent's view of lashing: call a tool, get its structured result."""

    def __init__(self, client: Client) -> None:
        self.client = client

    async def __call__(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self.client.call_tool(name, arguments)
        text = "".join(getattr(block, "text", "") for block in result.content)
        if result.is_error:
            raise ToolFailed(text)
        if result.structured_content is not None:
            return dict(result.structured_content)
        loaded: dict[str, Any] = json.loads(text)
        return loaded


@asynccontextmanager
async def connect(service: Lashing, person: Person | None = None) -> AsyncIterator[Tools]:
    async with Client(build_server(service), elicitation_callback=person) as client:
        yield Tools(client)


def confirmed_at_carrier(sim: Simulator, origin: str = "CNSHA", destination: str = "NLRTM", units: int = 1) -> str:
    """A booking that already exists and is confirmed at the carrier, made without lashing."""
    request = json.loads((FIXTURES / "booking-dry-cargo.json").read_text())
    for key in ("vessel", "carrierExportVoyageNumber"):
        request.pop(key)
    request["shipmentLocations"] = [
        {"location": {"UNLocationCode": origin}, "locationTypeCode": "POL"},
        {"location": {"UNLocationCode": destination}, "locationTypeCode": "POD"},
    ]
    request["requestedEquipments"][0]["units"] = units
    reference = sim.desk.submit(request)
    sim.desk.process()
    booking = sim.desk.find(reference)
    assert booking.booking_reference is not None, booking.feedbacks
    return booking.booking_reference
