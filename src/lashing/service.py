"""lashing's core: read bookings and schedules, propose writes as plans, and apply plans safely.

The MCP server and the CLI are thin layers over `Lashing`. Every carrier write goes through
`apply`, which enforces the invariants the project exists for:

1. a write is only ever a validated plan (the exact DCSA body, checked against the schema);
2. a plan is applied at most once, across processes;
3. a plan whose booking changed since it was proposed is refused;
4. a write needs a grant from the config, or an approval that did not come from the model;
5. nothing the carrier writes can change what is authorized;
6. every step is in the hash-chained ledger.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lashing import views
from lashing.carrier import Carrier, CarrierError, NotFound, NotYetProcessed
from lashing.config import Config, Scope
from lashing.dcsa.booking import (
    AmendmentStatus,
    BookingState,
    Cancellation,
    Change,
    LifecycleError,
    cancellation_payload,
)
from lashing.dcsa.schema import SchemaViolation, Spec, check
from lashing.ledger import Ledger
from lashing.plans import KINDS, Plan, PlanBook, PlanState
from lashing.plans import Change as FieldChange

# Common names people and models use, mapped to ISO 6346 size-type codes.
EQUIPMENT_ALIASES = {
    "20GP": "22G1", "20DV": "22G1", "20DC": "22G1", "20ST": "22G1",
    "40GP": "42G1", "40DV": "42G1", "40DC": "42G1", "40ST": "42G1",
    "40HC": "45G1", "40HQ": "45G1",
    "20RF": "22R1", "20RE": "22R1",
    "40RF": "45R1", "40RH": "45R1", "40HR": "45R1",
}  # fmt: skip
_ISO_CODE = re.compile(r"^[0-9]{2}[A-Z][0-9A-Z]$")
_CONTAINER = re.compile(r"^[A-Z]{3}[UJZ][0-9]{7}$")
_PORT = re.compile(r"^[A-Z]{2}[A-Z2-9]{3}$")

# Booking fields the carrier sets; they are never sent back in an update or amendment.
CARRIER_FIELDS = frozenset(
    {
        "bookingStatus", "amendedBookingStatus", "bookingCancellationStatus", "feedbacks",
        "confirmedEquipments", "transportPlan", "shipmentCutOffTimes", "charges", "advanceManifestFilings",
        "carrierBookingRequestReference", "carrierBookingReference",
    },
)  # fmt: skip
VOYAGE_FIELDS = (
    "vessel", "carrierExportVoyageNumber", "universalExportVoyageReference",
    "carrierServiceCode", "carrierServiceName", "universalServiceReference",
)  # fmt: skip

Approver = Callable[[Plan], Awaitable[bool | None]]
"""Asks a person about a plan: True to approve, False to decline, None if nobody can be asked."""


class InvalidRequest(ValueError):
    """The agent asked for something malformed; the message says how to fix it."""


@dataclass(frozen=True)
class EquipmentLine:
    type: str
    units: int
    commodity: str | None = None
    cargo_weight_kg_per_container: float | None = None


def iso_code(value: str) -> str:
    code = value.strip().upper().replace(" ", "").replace("'", "")
    code = EQUIPMENT_ALIASES.get(code, code)
    if not _ISO_CODE.match(code):
        known = ", ".join(sorted(EQUIPMENT_ALIASES))
        raise InvalidRequest(f"{value!r} is not an ISO 6346 size-type code (e.g. 22G1, 45G1) or one of: {known}")
    return code


def port(value: str, what: str) -> str:
    code = value.strip().upper()
    if not _PORT.match(code):
        raise InvalidRequest(f"{what} must be a UN/LOCODE such as CNSHA or NLRTM, not {value!r}")
    return code


def fingerprint(booking: dict[str, Any]) -> str:
    canonical = json.dumps(booking, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _places(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    found: dict[str, str] = {}
    for item in payload.get("shipmentLocations", []):
        code = item.get("location", {}).get("UNLocationCode")
        if code:
            found.setdefault(item.get("locationTypeCode", ""), code)
    return found.get("POL") or found.get("PRE"), found.get("POD") or found.get("PDE")


def _lane(payload: dict[str, Any]) -> str | None:
    origin, destination = _places(payload)
    return f"{origin}-{destination}" if origin and destination else None


def _units(payload: dict[str, Any]) -> int:
    return sum(int(e.get("units", 0)) for e in payload.get("requestedEquipments", []))


def _equipment_text(lines: list[dict[str, Any]]) -> str:
    parts = []
    for line in views.equipment(lines):
        text = f"{line['units']} x {line['type']}"
        if line["commodity"]:
            text += f" of {line['commodity']}"
        if line["cargo_weight_kg_per_container"] is not None:
            text += f" ({line['cargo_weight_kg_per_container']:,.0f} kg each)"
        parts.append(text)
    return ", ".join(parts)


@dataclass(frozen=True)
class Outcome:
    status: str  # applied | needs_approval | refused | failed | already_applied | in_progress
    plan_id: str
    message: str
    authorized_by: str | None = None
    booking: dict[str, Any] | None = None

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "plan_id": self.plan_id, "message": self.message}
        if self.authorized_by:
            out["authorized_by"] = self.authorized_by
        if self.booking is not None:
            out["booking"] = self.booking
        return out


class Lashing:
    def __init__(
        self,
        config: Config,
        carrier: Carrier,
        *,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.config = config
        self.carrier = carrier
        self.clock = clock
        self.ledger = Ledger(config.state_dir / "ledger.jsonl")
        self.plans = PlanBook(self.ledger)

    # -- reading -----------------------------------------------------------------------------------

    async def find_sailings(
        self,
        origin: str,
        destination: str,
        depart_from: dt.date | None = None,
        depart_until: dt.date | None = None,
        max_transshipments: int = 1,
    ) -> dict[str, Any]:
        routes = await self.carrier.routes(
            port(origin, "origin"),
            port(destination, "destination"),
            departure_from=depart_from,
            departure_until=depart_until,
            max_transshipments=max_transshipments,
        )
        return {
            "sailings": [views.sailing(r) for r in routes],
            "how_to_book": "Pass a sailing's routing_reference to propose_booking or propose_change.",
        }

    async def _current(self, reference: str) -> dict[str, Any]:
        try:
            return await self.carrier.get_booking(reference)
        except NotYetProcessed:
            raise InvalidRequest(f"the carrier has not processed {reference} yet; try again shortly") from None
        except NotFound:
            raise InvalidRequest(f"the carrier has no booking {reference!r}") from None

    async def booking(self, reference: str) -> dict[str, Any]:
        current = await self._current(reference)
        state = BookingState.from_payload(current)
        amended = None
        if state.amendment is AmendmentStatus.AMENDMENT_RECEIVED and state.booking_reference:
            try:
                amended = await self.carrier.get_booking(state.booking_reference, amended=True)
            except NotFound:
                amended = None
        view = views.booking(current, amended)
        open_plans = [
            s.plan.view() for s in self.plans.open() if set(s.plan.scope.references) & {reference, state.label}
        ]
        if open_plans:
            view["open_plans"] = open_plans
        return view

    async def track(self, reference: str) -> dict[str, Any]:
        reference = reference.strip()
        if _CONTAINER.match(reference.upper()):
            events = await self.carrier.events(equipment_reference=reference.upper())
            return views.tracking(reference.upper(), events)
        state = BookingState.from_payload(await self._current(reference))
        if not state.booking_reference:
            return {"reference": reference, "message": "Tracking starts once the carrier confirms the booking."}
        events = await self.carrier.events(booking_reference=state.booking_reference)
        return views.tracking(state.booking_reference, events)

    def bookings(self) -> list[dict[str, str]]:
        """Bookings lashing has written to, newest first, from the ledger."""
        seen: dict[str, dict[str, str]] = {}
        aliases: dict[str, str] = {}
        for entry in self.ledger.entries():
            if entry["kind"] != "applied" or not entry.get("reference"):
                continue
            label = entry.get("booking") or entry["reference"]
            key = aliases.setdefault(entry["reference"], label)
            aliases.setdefault(label, key)
            seen.pop(key, None)
            seen[key] = {"reference": label, "last_action": entry.get("action", "")}
        return list(reversed(seen.values()))

    def open_plans(self) -> list[dict[str, Any]]:
        return [s.plan.view() for s in self.plans.open()]

    # -- proposing ---------------------------------------------------------------------------------

    def _plan(
        self,
        kind: str,
        summary: str,
        payload: dict[str, Any],
        scope: Scope,
        *,
        reference: str | None = None,
        booking: dict[str, Any] | None = None,
        changes: tuple[FieldChange, ...] = (),
    ) -> dict[str, Any]:
        now = self.clock()
        plan = Plan(
            id=Plan.new_id(),
            kind=kind,
            summary=summary,
            payload=payload,
            scope=scope,
            created_at=now,
            expires_at=now + self.config.plan_ttl,
            reference=reference,
            fingerprint=fingerprint(booking) if booking is not None else None,
            changes=changes,
        )
        self.plans.propose(plan)
        view = plan.view()
        grant = self.config.grant_for(scope, now.date())
        if grant is not None:
            view["authorization"] = f"covered by grant {grant.id!r}; apply_plan will send it"
        else:
            ways = []
            if self.config.approvals.client:
                ways.append("apply_plan will ask the person using this client to approve")
            if self.config.approvals.operator:
                ways.append(f"an operator can run `lashing approve {plan.id}`")
            view["authorization"] = "needs a person's approval: " + (
                "; or ".join(ways) or "no approval channel is enabled"
            )
        view["next_step"] = f"Call apply_plan with plan_id {plan.id} to send this to the carrier."
        return view

    def _validated(self, component: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            check(Spec.BOOKING, component, payload)
        except SchemaViolation as violation:
            raise InvalidRequest(str(violation)) from None
        return payload

    def _equipment_payload(
        self,
        lines: list[EquipmentLine],
        existing: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not lines:
            raise InvalidRequest("at least one equipment line is needed")
        out = []
        for line in lines:
            if line.units < 1:
                raise InvalidRequest("units must be at least 1")
            code = iso_code(line.type)
            previous = next((e for e in existing or [] if e.get("ISOEquipmentCode") == code), None)
            previous = previous or next(iter(existing or []), None)
            old_commodity = (previous or {}).get("commodities", [{}])[0] if previous else {}
            commodity: dict[str, Any] = {
                "commodityType": line.commodity or old_commodity.get("commodityType") or "General cargo",
            }
            # DCSA's cargoGrossWeight is the total for the whole line; people think per container.
            per_container = line.cargo_weight_kg_per_container
            if per_container is None and previous is not None:
                per_container = views.weight_per_container(previous)
            if per_container is not None:
                if per_container <= 0:
                    raise InvalidRequest("cargo_weight_kg_per_container must be positive")
                total = round(float(per_container) * line.units, 3)
                commodity["cargoGrossWeight"] = {"value": total, "unit": "KGM"}
            out.append(
                {
                    "ISOEquipmentCode": code,
                    "units": line.units,
                    "isShipperOwned": bool((previous or {}).get("isShipperOwned", False)),
                    "commodities": [commodity],
                },
            )
        return out

    async def propose_booking(
        self,
        origin: str,
        destination: str,
        equipment: list[EquipmentLine],
        *,
        routing_reference: str | None = None,
        depart_from: dt.date | None = None,
        special_instructions: str | None = None,
    ) -> dict[str, Any]:
        origin, destination = port(origin, "origin"), port(destination, "destination")
        shipper = self.config.shipper
        payload: dict[str, Any] = {
            "receiptTypeAtOrigin": "CY",
            "deliveryTypeAtDestination": "CY",
            "cargoMovementTypeAtOrigin": "FCL",
            "cargoMovementTypeAtDestination": "FCL",
            "isEquipmentSubstitutionAllowed": False,
            "documentParties": shipper.document_parties(),
            "shipmentLocations": [
                {"location": {"UNLocationCode": origin}, "locationTypeCode": "POL"},
                {"location": {"UNLocationCode": destination}, "locationTypeCode": "POD"},
            ],
            "requestedEquipments": self._equipment_payload(equipment),
        }
        if shipper.service_contract:
            payload["serviceContractReference"] = shipper.service_contract
        if shipper.freight_payment:
            payload["freightPaymentTermCode"] = shipper.freight_payment
        if routing_reference:
            payload["routingReference"] = routing_reference
        if depart_from:
            payload["expectedDepartureDate"] = depart_from.isoformat()
        if special_instructions:
            payload["specialInstructions"] = special_instructions
        self._validated("CreateBooking", payload)
        when = f"on sailing {routing_reference}" if routing_reference else "on the carrier's best sailing"
        if depart_from and not routing_reference:
            when += f" departing from {depart_from.isoformat()}"
        summary = f"Book {_equipment_text(payload['requestedEquipments'])} from {origin} to {destination} {when}."
        scope = Scope(action="create", lane=f"{origin}-{destination}", units=_units(payload))
        return self._plan("create", summary, payload, scope)

    async def propose_change(
        self,
        reference: str,
        *,
        routing_reference: str | None = None,
        depart_from: dt.date | None = None,
        equipment: list[EquipmentLine] | None = None,
        special_instructions: str | None = None,
    ) -> dict[str, Any]:
        if routing_reference is None and depart_from is None and equipment is None and special_instructions is None:
            raise InvalidRequest(
                "say what to change: routing_reference, depart_from, equipment or special_instructions"
            )
        current = await self._current(reference)
        state = BookingState.from_payload(current)
        try:
            change = state.require_change()
            path_reference = state.path_reference(change)
        except LifecycleError as error:
            raise InvalidRequest(str(error)) from None
        base = current
        if change is Change.AMEND and state.amendment is AmendmentStatus.AMENDMENT_RECEIVED:
            base = await self.carrier.get_booking(path_reference, amended=True)  # a new amendment replaces it
        content = {k: v for k, v in copy.deepcopy(base).items() if k not in CARRIER_FIELDS}
        updated = copy.deepcopy(content)
        if routing_reference:
            for key in VOYAGE_FIELDS:
                updated.pop(key, None)
            updated["routingReference"] = routing_reference
        elif depart_from:
            for key in (*VOYAGE_FIELDS, "routingReference"):
                updated.pop(key, None)
            updated["expectedDepartureDate"] = depart_from.isoformat()
        if equipment is not None:
            updated["requestedEquipments"] = self._equipment_payload(equipment, content.get("requestedEquipments"))
        if special_instructions is not None:
            updated["specialInstructions"] = special_instructions
        changed = sorted(k for k in set(content) | set(updated) if content.get(k) != updated.get(k))
        if not changed:
            raise InvalidRequest("that is what the booking already says; nothing to change")
        self._validated("UpdateBooking", updated)

        def shown(key: str, source: dict[str, Any]) -> Any:
            if key == "requestedEquipments":
                return views.equipment(source.get(key, []))
            return source.get(key)

        changes = tuple(FieldChange(k, shown(k, content), shown(k, updated)) for k in changed)
        verb = "Update the request" if change is Change.UPDATE else "Amend confirmed booking"
        summary = f"{verb} {state.label}: " + "; ".join(
            f"{c.field} {c.before!r} -> {c.after!r}" if c.field != "requestedEquipments" else
            f"equipment -> {_equipment_text(updated['requestedEquipments'])}"
            for c in changes
        ) + "."  # fmt: skip
        scope = Scope(
            action=change.value,
            references=tuple(r for r in (state.request_reference, state.booking_reference) if r),
            lane=_lane(updated),
            fields=frozenset(changed),
            units=_units(updated),
        )
        return self._plan(
            change.value,
            summary,
            updated,
            scope,
            reference=path_reference,
            booking=current,
            changes=changes,
        )

    async def propose_cancellation(
        self, reference: str, reason: str, *, amendment_only: bool = False
    ) -> dict[str, Any]:
        if not reason.strip():
            raise InvalidRequest("give the reason for cancelling; the carrier receives it")
        current = await self._current(reference)
        state = BookingState.from_payload(current)
        try:
            kind = state.require_cancellation(amendment_only=amendment_only)
            path_reference = state.path_reference(kind)
        except LifecycleError as error:
            raise InvalidRequest(str(error)) from None
        payload = cancellation_payload(kind, reason.strip())
        what = {
            Cancellation.REQUEST: "Cancel the booking request",
            Cancellation.CONFIRMED: "Ask the carrier to cancel confirmed booking",
            Cancellation.AMENDMENT: "Withdraw the pending amendment to booking",
        }[kind]
        summary = f"{what} {state.label} (reason: {reason.strip()})."
        scope = Scope(
            action="cancel",
            references=tuple(r for r in (state.request_reference, state.booking_reference) if r),
            lane=_lane(current),
            units=_units(current),
        )
        return self._plan(kind.value, summary, payload, scope, reference=path_reference, booking=current)

    def discard(self, plan_id: str) -> dict[str, Any]:
        state = self.plans.get(plan_id)
        if state is None or state.status != "proposed":
            raise InvalidRequest(f"there is no open plan {plan_id!r}")
        self.plans.close(plan_id, "discarded")
        return {"plan_id": plan_id, "status": "discarded"}

    # -- applying ----------------------------------------------------------------------------------

    async def apply(self, plan_id: str, approver: Approver | None = None) -> Outcome:
        state = self.plans.get(plan_id)
        if state is None or state.status != "proposed":
            return self._not_open(plan_id, state)
        plan = state.plan
        if (refusal := await self._still_valid(plan)) is not None:
            return refusal
        authorized_by = await self._authorize(plan, state.approved_by, approver)
        if authorized_by is None or authorized_by == "declined":
            return self._unauthorized(plan, declined=authorized_by == "declined")
        if not self.plans.claim(plan_id, authorized_by):
            return Outcome("in_progress", plan_id, "Another process is applying or has closed this plan.")
        try:
            reference = await self._send(plan)
        except (CarrierError, SchemaViolation) as error:
            self.plans.close(plan_id, "failed", error=str(error), authorized_by=authorized_by)
            return Outcome("failed", plan_id, f"The carrier did not accept the request: {error}", authorized_by)
        try:
            after = views.booking(await self.carrier.get_booking(reference))
        except CarrierError:
            after = None
        label = after["reference"] if after else reference
        self.plans.close(
            plan_id, "applied", reference=reference, booking=label, action=plan.kind, authorized_by=authorized_by
        )
        message = "Sent to the carrier."
        if after is None:
            message += " The carrier processes it asynchronously; check get_booking."
        return Outcome("applied", plan_id, message, authorized_by, after)

    @staticmethod
    def _not_open(plan_id: str, state: PlanState | None) -> Outcome:
        if state is None:
            return Outcome("refused", plan_id, f"There is no plan {plan_id!r}. Propose the change first.")
        if state.status == "applied":
            by = (state.outcome or {}).get("authorized_by")
            return Outcome("already_applied", plan_id, "This plan was already applied; it is never sent twice.", by)
        if state.status == "applying":
            return Outcome("in_progress", plan_id, "This plan is being applied now; it will not be sent again.")
        return Outcome("refused", plan_id, f"This plan is {state.status}. Propose the change again.")

    async def _still_valid(self, plan: Plan) -> Outcome | None:
        """Refuse a plan that has expired, or whose booking changed at the carrier since it was proposed."""
        if self.clock() > plan.expires_at:
            self.plans.close(plan.id, "expired")
            return Outcome("refused", plan.id, "This plan expired. Propose the change again.")
        if plan.fingerprint is None or plan.reference is None:
            return None
        try:
            current = await self.carrier.get_booking(plan.reference)
        except CarrierError as error:
            return Outcome("failed", plan.id, f"Could not re-read the booking before applying: {error}")
        if fingerprint(current) != plan.fingerprint:
            self.plans.close(plan.id, "stale")
            message = "The booking changed at the carrier since this plan was made. Read it again and propose anew."
            return Outcome("refused", plan.id, message)
        return None

    async def approval_question(self, plan_id: str) -> str | None:
        """The question to put to a person before applying, or None when no person should be asked.

        Nobody is asked about a plan that cannot be applied anyway, or one a grant or an operator's
        approval already covers.
        """
        state = self.plans.get(plan_id)
        if state is None or state.status != "proposed" or not self.config.approvals.client:
            return None
        if self.clock() > state.plan.expires_at:
            return None
        if self.config.grant_for(state.plan.scope, self.clock().date()) is not None:
            return None
        if state.approved_by and self.config.approvals.operator:
            return None
        return f"lashing wants to send this to the carrier:\n\n{state.plan.summary}\n\nPlan {plan_id}."

    async def _authorize(self, plan: Plan, approved_by: str | None, approver: Approver | None) -> str | None:
        grant = self.config.grant_for(plan.scope, self.clock().date())
        if grant is not None:
            return f"grant:{grant.id}"
        if approved_by and self.config.approvals.operator:
            return f"approved:{approved_by}"
        if self.config.approvals.client and approver is not None:
            decision = await approver(plan)
            if decision is True:
                self.plans.approve(plan.id, by="client")
                return "approved:client"
            if decision is False:
                return "declined"
        return None

    def _unauthorized(self, plan: Plan, *, declined: bool) -> Outcome:
        if declined:
            self.plans.close(plan.id, "refused", by="person")
            return Outcome("refused", plan.id, "The person declined this plan. It will not be sent.")
        self.ledger.append("awaiting_approval", plan_id=plan.id)
        ways = []
        if self.config.approvals.operator:
            ways.append(f"an operator runs `lashing approve {plan.id}` and you call apply_plan again")
        if self.config.approvals.client:
            ways.append("the client supports approval prompts and a person approves")
        how = "; or ".join(ways) if ways else "a grant in the config covers it (no approval channel is enabled)"
        return Outcome("needs_approval", plan.id, f"Not sent. This needs a person's approval: {how}.")

    async def _send(self, plan: Plan) -> str:
        if plan.kind == "create":
            return await self.carrier.create_booking(plan.payload)
        if plan.reference is None:  # pragma: no cover - proposals always set it for these kinds
            raise LifecycleError(f"{plan.kind} plan has no booking reference")
        if plan.kind in ("update", "amend"):
            await self.carrier.update_booking(plan.reference, plan.payload)
        elif KINDS.get(plan.kind) == "cancel":
            await self.carrier.cancel_booking(plan.reference, plan.payload)
        else:  # pragma: no cover
            raise LifecycleError(f"unknown plan kind {plan.kind!r}")
        return plan.reference
