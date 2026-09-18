"""Run the DCSA Booking 2.0 conformance scenarios against lashing, headlessly.

The DCSA Conformance Framework (github.com/dcsaorg/Conformance-Gateway) runs locally with
`docker compose up -d`: the backend on :8080, the web UI on :4200. The UI drives the backend
through one JSON endpoint, POST /conformance/webui, and so does this script.

  carrier  The framework plays the shipper; the lashing simulator is the carrier under test.
           Start it with `uv run lashing sim --port 8401 --manual`, so it decides nothing until
           this script performs the use case the framework asks for.
  shipper  The framework plays the carrier; lashing's HttpCarrier is the shipper under test.

Every scenario is started and each prompt answered: the booking is supplied, the carrier's use
case performed through the simulator's /_sim controls, or the shipper's call made with
HttpCarrier. Each action is then marked completed, as a person would in the web UI. The script
prints every action's check counts and every failing check, and writes the framework's JSON
report to --report.

Run it from the lashing repository (see conformance/README.md):

  uv run python conformance/run_conformance.py carrier
  uv run python conformance/run_conformance.py shipper --match 'Dry cargo'
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol

import httpx

from lashing.carrier import CarrierError, Endpoints, HttpCarrier
from lashing.dcsa.booking import BookingState, Change, LifecycleError, cancellation_payload, update_body

STANDARD, VERSION, SUITE = "Booking", "2.0.0", "Conformance"  # the framework's name for Booking 2.0.x
FAILED = frozenset({"NON_CONFORMANT", "PARTIALLY_CONFORMANT"})
POLL_SECONDS = 0.3
STEP_TIMEOUT = 30.0  # how long the framework's own party may take to act


class GatewayError(RuntimeError):
    """The framework refused an operation; the message is its own."""


class Unsupported(RuntimeError):
    """A prompt this script cannot answer; the scenario is stopped."""


# -- the framework --------------------------------------------------------------------------------


@dataclass
class Sandbox:
    """One sandbox in the Conformance Gateway, reached through its web-UI API."""

    http: httpx.AsyncClient
    id: str
    config: dict[str, Any]

    @staticmethod
    async def call(http: httpx.AsyncClient, operation: str, **fields: Any) -> Any:
        response = await http.post("/conformance/webui", json={"operation": operation, **fields})
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict) and "error" in body:
            raise GatewayError(f"{operation}: {body['error']}")
        return body

    @classmethod
    async def create(cls, http: httpx.AsyncClient, role: str, external_url: str) -> Sandbox:
        """A sandbox testing `role`; the framework's own party calls `external_url` (may be empty)."""
        name = f"lashing {role.lower()} {dt.datetime.now():%Y-%m-%d %H:%M:%S}"
        created = await cls.call(
            http,
            "createSandbox",
            standardName=STANDARD,
            versionNumber=VERSION,
            scenarioSuite=SUITE,
            testedPartyRole=role,
            isDefaultType=True,  # "Test orchestrator and counterparts (default)"
            sandboxName=name,
        )
        sandbox_id = created["sandboxId"]
        await cls.call(
            http,
            "updateSandboxConfig",
            sandboxId=sandbox_id,
            sandboxName=name,
            externalPartyUrl=external_url,
            externalPartyAuthHeaderName="",
            externalPartyAuthHeaderValue="",
            externalPartyAdditionalHeaders=[],
            externalPartyEndpointUriOverrides=[],
        )
        return cls(http, sandbox_id, await cls.call(http, "getSandboxConfig", sandboxId=sandbox_id))

    async def op(self, operation: str, **fields: Any) -> Any:
        return await self.call(self.http, operation, sandboxId=self.id, **fields)

    async def scenarios(self) -> list[Scenario]:
        modules = await self.op("getScenarioDigests")
        return [Scenario(m["moduleName"], s["id"], s["name"]) for m in modules for s in m["scenarios"]]

    async def wait_idle(self) -> None:
        """Wait until neither party is busy with an outbound call."""
        async with asyncio.timeout(STEP_TIMEOUT):
            while (await self.op("getSandboxStatus")).get("waiting"):
                await asyncio.sleep(POLL_SECONDS)

    async def json_report(self, title: str) -> dict[str, Any]:
        await self.op("createReport", reportTitle=title)
        latest = (await self.op("getReportDigests"))[0]
        report: dict[str, Any] = await self.op("getReportContent", reportIsoTimestamp=latest["isoTimestamp"])
        return report


@dataclass(frozen=True)
class Scenario:
    module: str
    id: str
    name: str


@dataclass(frozen=True)
class Prompt:
    """What the framework asks the tested party to do next, from its human-readable prompt."""

    action_id: str
    text: str
    input_required: bool

    @classmethod
    def of(cls, status: dict[str, Any]) -> Prompt | None:
        """The tested party's prompt, or None when the framework's own party acts next."""
        if "promptActionId" not in status:
            return None
        return cls(status["promptActionId"], status.get("promptText", ""), bool(status.get("inputRequired")))

    def _match(self, pattern: str) -> str | None:
        found = re.search(pattern, self.text, re.IGNORECASE)
        return found.group(1) if found else None

    @property
    def title(self) -> str:
        return self.text.splitlines()[0] if self.text else "(no prompt text)"

    @property
    def use_case(self) -> int | None:
        number = self._match(r"use case (\d+)")
        return int(number) if number else None

    @property
    def is_get(self) -> bool:
        return self.text.startswith("Perform a GET")

    @property
    def declines(self) -> bool:
        return self._match(r"use case \d+: (decline)") is not None

    @property
    def cargo(self) -> Cargo:
        """The cargo asked for ("Submit a Reefer booking request", "remains DG"); dry when any will do."""
        named = re.search(r"\b(Dry cargo|Reefer|DG)\b", self.text)
        return Cargo(named.group(1)) if named else Cargo.DRY

    @property
    def reference(self) -> str:
        """The booking the prompt is about: its carrierBookingReference once known, else the request's."""
        reference = self._match(r"CBR '([^']+)'") or self._match(r"CBRR '([^']+)'")
        if reference is None:
            raise Unsupported(f"no booking reference in prompt: {self.title}")
        return reference


# -- bookings -------------------------------------------------------------------------------------


class Cargo(StrEnum):
    """The cargo types the framework's scenarios qualify a booking by."""

    DRY = "Dry cargo"
    REEFER = "Reefer"
    DG = "DG"


AEROSOLS = {
    "UNNumber": "1950",
    "properShippingName": "AEROSOLS,NON-FLAMMABLE",
    "imoClass": "2.2",
    "isMarinePollutant": False,
    "isLimitedQuantity": False,
    "isExceptedQuantity": False,
    "isSalvagePackings": False,
    "isEmptyUncleanedResidue": False,
    "isWaste": False,
    "isHot": False,
    "isCompetentAuthorityApprovalRequired": False,
    "emergencyContactDetails": {"contact": "Josephine Jackson", "phone": "+4570262970"},
    "isReportableQuantity": False,
    "grossWeight": {"value": 6.28, "unit": "KGM"},
}  # from the framework's own DG example booking


def booking_request(cargo: Cargo, departure: dt.date) -> dict[str, Any]:
    """A CreateBooking for one 20ft container from Singapore to Rotterdam, sailing on or after `departure`.

    The framework's example bookings name a vessel and voyage that no carrier sails. Asking for a
    departure date instead lets the carrier pick the sailing, and still meets the framework's
    rule that a booking must give either a voyage or dates.
    """
    commodity: dict[str, Any] = {
        "commodityType": {Cargo.DRY: "Dry cargo", Cargo.REEFER: "Reefer cargo", Cargo.DG: "Dangerous goods"}[cargo]
        + ", Freight all kinds",
        "cargoGrossWeight": {"value": 3000.0, "unit": "KGM"},
    }
    equipment: dict[str, Any] = {"ISOEquipmentCode": "22GP", "units": 1, "isShipperOwned": False}
    if cargo is Cargo.REEFER:
        equipment["ISOEquipmentCode"] = "22RT"
        equipment["isNonOperatingReefer"] = False
        equipment["activeReeferSettings"] = {"temperatureSetpoint": 5.0, "temperatureUnit": "CEL"}
    if cargo is Cargo.DG:
        commodity["outerPackaging"] = {"numberOfPackages": 1, "packageCode": "2Q", "dangerousGoods": [AEROSOLS]}
    equipment["commodities"] = [commodity]
    return {
        "receiptTypeAtOrigin": "CY",
        "deliveryTypeAtDestination": "CY",
        "cargoMovementTypeAtOrigin": "FCL",
        "cargoMovementTypeAtDestination": "FCL",
        "serviceContractReference": "HHL51800000",
        "isEquipmentSubstitutionAllowed": True,
        "documentParties": {"bookingAgent": {"partyName": "DCSA Team"}},
        "expectedDepartureDate": departure.isoformat(),
        "shipmentLocations": [
            {"location": {"UNLocationCode": "SGSIN"}, "locationTypeCode": "POL"},
            {"location": {"UNLocationCode": "NLRTM"}, "locationTypeCode": "POD"},
        ],
        "requestedEquipments": [equipment],
    }


# -- the party under test -------------------------------------------------------------------------


class TestedParty(Protocol):
    role: str

    async def booking(self, cargo: Cargo) -> dict[str, Any]:
        """The booking to supply when the framework asks for one (the carrier's SupplyCSP)."""
        ...

    async def perform(self, prompt: Prompt) -> str:
        """Do what the prompt asks; returns a line describing what was done."""
        ...


class SimulatedCarrier:
    """The lashing simulator as the carrier: each carrier use case is performed through /_sim."""

    role = "Carrier"

    # (use case, prompt says "Decline") -> the override that steers /_sim/process, or None when
    # processing alone does it (confirming a booking, an amendment or a cancellation).
    STEPS: ClassVar[dict[tuple[int, bool], str | None]] = {
        (2, False): "request_update",
        (4, False): "reject",
        (5, False): None,
        (6, False): "request_amendment",
        (8, False): None,
        (8, True): "decline_amendment",
        (10, True): "decline",
        (14, False): None,
        (14, True): "decline_cancellation",
    }
    COMPLETE = 12  # "complete the booking" has its own control rather than an override

    def __init__(self, sim: httpx.AsyncClient) -> None:
        self.sim = sim

    async def _control(self, control: str, body: dict[str, str]) -> None:
        response = await self.sim.post(f"/_sim/{control}", json=body)
        if response.is_error:
            raise Unsupported(f"simulator refused /_sim/{control}: {response.text}")

    async def booking(self, cargo: Cargo) -> dict[str, Any]:
        state = (await self.sim.get("/_sim/state")).json()
        today = dt.datetime.fromisoformat(state["now"]).date()
        return booking_request(cargo, today + dt.timedelta(days=7))

    async def perform(self, prompt: Prompt) -> str:
        step = (prompt.use_case or 0, prompt.declines)
        if step[0] == self.COMPLETE:
            await self._control("complete", {"reference": prompt.reference})
            return f"UC12 completed {prompt.reference}"
        if step not in self.STEPS:
            raise Unsupported(f"the simulator has no control for: {prompt.title}")
        reference = prompt.reference
        if override := self.STEPS[step]:
            await self._control("override", {"reference": reference, "action": override})
        await self._control("process", {"reference": reference})
        return f"UC{step[0]} on {reference}" + (f" (override {override})" if override else " (processed)")


class LashingShipper:
    """lashing's HttpCarrier as the shipper, calling the carrier the framework runs."""

    role = "Shipper"

    def __init__(self, carrier: HttpCarrier) -> None:
        self.carrier = carrier

    async def booking(self, cargo: Cargo) -> dict[str, Any]:
        return booking_request(cargo, dt.date.today() + dt.timedelta(days=14))

    async def perform(self, prompt: Prompt) -> str:
        if prompt.is_get:
            amended = "AMENDED content" in prompt.text
            booking = await self.carrier.get_booking(prompt.reference, amended=amended)
            return f"GET {prompt.reference}{' (amended)' if amended else ''}: {booking.get('bookingStatus')}"
        if prompt.use_case == 1:
            booking = await self.booking(prompt.cargo)
            return f"UC1 created a {prompt.cargo} booking: {await self.carrier.create_booking(booking)}"
        # Changes and cancellations go through lashing's own lifecycle rules, as the service does:
        # read the booking, let BookingState pick the action and the reference the path must carry.
        current = await self.carrier.get_booking(prompt.reference)
        state = BookingState.from_payload(current)
        if prompt.use_case in (3, 7):
            change = state.require_change()
            payload = update_body(current)  # the same body lashing's propose_change starts from
            payload["specialInstructions"] = f"Changed by lashing for DCSA conformance (UC{prompt.use_case})."
            reference = state.path_reference(change)
            await self.carrier.update_booking(reference, payload)
            return f"UC{prompt.use_case} {'updated' if change is Change.UPDATE else 'amended'} {reference}"
        if prompt.use_case in (9, 11, 13):
            kind = state.require_cancellation(amendment_only=prompt.use_case == 9)
            reference = state.path_reference(kind)
            await self.carrier.cancel_booking(reference, cancellation_payload(kind, "DCSA conformance run"))
            return f"UC{prompt.use_case} {kind.value} on {reference}"
        raise Unsupported(f"no shipper step for: {prompt.title}")


# -- running scenarios ----------------------------------------------------------------------------


async def run_scenario(sandbox: Sandbox, scenario: Scenario, party: TestedParty) -> str | None:
    """Run one scenario to its end; returns why it was abandoned, or None when it ran through."""
    await sandbox.op("startOrStopScenario", scenarioId=scenario.id)
    try:
        idle_since = asyncio.get_running_loop().time()
        while True:
            await sandbox.wait_idle()
            status = await sandbox.op("getScenarioStatus", scenarioId=scenario.id)
            if not status.get("isRunning"):
                return None
            step = status["nextActions"].split(" - ")[0]
            prompt = Prompt.of(status)
            if prompt is None:
                # The framework's own party acts. Its shipper's request must be marked completed;
                # its carrier's use cases complete themselves.
                exchange = (await sandbox.op("getCurrentActionExchanges", scenarioId=scenario.id)).get(
                    "primaryExchange"
                )
                if exchange is None:
                    if asyncio.get_running_loop().time() - idle_since > STEP_TIMEOUT:
                        return f"{step}: the framework's own party did nothing in {STEP_TIMEOUT:.0f} s"
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                request, response = exchange["request"], exchange["response"]
                path = request["url"][request["url"].find("/v2/") :]
                print(f"    {step:<22} framework: {request['method']} {path} -> {response['statusCode']}")
            elif prompt.input_required:
                await sandbox.op(
                    "handleActionInput",
                    scenarioId=scenario.id,
                    actionId=prompt.action_id,
                    actionInput=await party.booking(prompt.cargo),
                )
                print(f"    {step:<22} supplied a {prompt.cargo} booking")
                idle_since = asyncio.get_running_loop().time()
                continue
            else:
                try:
                    done = await party.perform(prompt)
                except (CarrierError, LifecycleError) as error:
                    done = f"failed: {error}"
                print(f"    {step:<22} {party.role.lower()}: {done}")
                await sandbox.wait_idle()
            await sandbox.op("completeCurrentAction", skip=False)
            idle_since = asyncio.get_running_loop().time()
    except (GatewayError, Unsupported, TimeoutError) as problem:
        return str(problem) or type(problem).__name__
    finally:
        if (await sandbox.op("getScenarioStatus", scenarioId=scenario.id)).get("isRunning"):
            await sandbox.op("startOrStopScenario", scenarioId=scenario.id)  # stops it


def nodes(report: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for child in report.get("subReports") or []:
        yield child
        yield from nodes(child)


def failures(report: dict[str, Any]) -> list[str]:
    """Every failing check under `report`, with the framework's messages."""
    lines = []
    for node in nodes(report):
        if node["status"] in FAILED and (node.get("errorMessages") or not node.get("subReports")):
            messages = "; ".join(node.get("errorMessages") or [])
            lines.append(node["title"].strip() + (f": {messages}" if messages else ""))
    return lines


def summarize(report: dict[str, Any], verbose: bool) -> list[str]:
    """One line per action with its checks counted by status, then each failing check."""
    lines = []
    for action in report.get("subReports") or []:
        checks = [n for n in nodes(action) if not n.get("subReports") and n["status"] != "IRRELEVANT"]
        counts = Counter(check["status"] for check in checks)
        tally = ", ".join(f"{n} {status.lower()}" for status, n in sorted(counts.items())) or "no checks"
        lines.append(f"    {action['status']:<26} {action['title']}  ({tally})")
        if verbose:
            lines += [f"      {check['status']:<24} {check['title'].strip()}" for check in checks]
        lines += [f"      FAIL {failure}" for failure in failures(action)]
    return lines


async def run(sandbox: Sandbox, party: TestedParty, patterns: list[str], verbose: bool, report: Path) -> int:
    """Run the matching scenarios; returns the exit code (0 when nothing failed)."""
    scenarios = [s for s in await sandbox.scenarios() if not patterns or any(re.search(p, s.name) for p in patterns)]
    print(f"{STANDARD} {VERSION} {SUITE}, {party.role} role, sandbox {sandbox.id}: {len(scenarios)} scenarios")
    failed = 0
    for scenario in scenarios:
        print(f"\n{scenario.module}\n  {scenario.name}")
        abandoned = await run_scenario(sandbox, scenario, party)
        result = (await sandbox.op("getScenarioStatus", scenarioId=scenario.id))["conformanceSubReport"]
        failing = failures(result)
        if abandoned or failing:
            failed += 1
        verdict = f"ABANDONED ({abandoned})" if abandoned else ("FAILING CHECKS" if failing else result["status"])
        print(f"  => {verdict}")
        print("\n".join(summarize(result, verbose)))
    full = await sandbox.json_report(f"lashing {party.role.lower()} run")
    report.write_text(json.dumps(full, indent=2))
    print(f"\n{len(scenarios) - failed}/{len(scenarios)} scenarios without failures; overall {full['status']}")
    print(f"JSON report: {report}")
    return 1 if failed else 0


async def main(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(base_url=args.gateway, timeout=60) as gateway:
        if args.role == "carrier":
            async with httpx.AsyncClient(base_url=args.sim, timeout=10) as sim:
                sandbox = await Sandbox.create(gateway, "Carrier", args.carrier_url)
                return await run(sandbox, SimulatedCarrier(sim), args.match, args.verbose, args.report)
        sandbox = await Sandbox.create(gateway, "Shipper", "")  # no notification endpoint
        url = sandbox.config["sandboxUrl"]
        carrier = HttpCarrier(
            Endpoints(booking=url, schedules=url, tracking=url),
            headers={sandbox.config["sandboxAuthHeaderName"]: sandbox.config["sandboxAuthHeaderValue"]},
        )
        try:
            return await run(sandbox, LashingShipper(carrier), args.match, args.verbose, args.report)
        finally:
            await carrier.aclose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway", default="http://localhost:8080", help="the framework's backend")
    parser.add_argument("--match", action="append", default=[], help="run only scenarios whose name matches (regex)")
    parser.add_argument("--report", type=Path, help="where to write the JSON report")
    parser.add_argument("--verbose", action="store_true", help="list every check, not only failing ones")
    roles = parser.add_subparsers(dest="role", required=True)
    carrier = roles.add_parser("carrier", help="test the lashing simulator as the carrier")
    carrier.add_argument("--sim", default="http://127.0.0.1:8401", help="the simulator, as this script reaches it")
    carrier.add_argument(
        "--carrier-url",
        default="http://host.docker.internal:8401/bkg",
        help="the simulator's Booking API, as the framework's container reaches it",
    )
    roles.add_parser("shipper", help="test lashing's HttpCarrier as the shipper")
    args = parser.parse_args()
    args.report = args.report or Path(__file__).parent / "out" / f"conformance-{args.role}.json"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
