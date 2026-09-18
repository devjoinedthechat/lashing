"""The ordinary jobs an agent does with lashing, end to end through a real MCP client."""

from __future__ import annotations

import datetime as dt

import pytest

from lashing.sim import Simulator

from .conftest import Factory, Person, ToolFailed, confirmed_at_carrier, connect, grant

pytestmark = pytest.mark.anyio

FURNITURE = {"type": "40HC", "units": 2, "commodity": "Flat-packed furniture", "cargo_weight_kg_per_container": 18000}


async def test_the_tools_an_agent_sees(make_lashing: Factory) -> None:
    async with connect(make_lashing()) as tools:
        listed = (await tools.client.list_tools()).tools
    names = {t.name for t in listed}
    assert names == {
        "find_sailings", "get_booking", "track_shipment", "list_bookings", "list_plans",
        "propose_booking", "propose_change", "propose_cancellation", "apply_plan", "discard_plan",
    }  # fmt: skip
    destructive = {t.name for t in listed if t.annotations and t.annotations.destructive_hint}
    assert destructive == {"apply_plan"}
    assert all(
        t.annotations and t.annotations.read_only_hint
        for t in listed
        if t.name.startswith(("find", "get", "track", "list"))
    )


async def test_book_a_sailing_under_a_grant(make_lashing: Factory, sim: Simulator) -> None:
    service = make_lashing(grant("create", lanes=("CNSHA-*",), max_units=4))
    async with connect(service) as tools:
        sailings = (await tools("find_sailings", origin="CNSHA", destination="NLRTM"))["sailings"]
        choice = sailings[0]
        plan = await tools(
            "propose_booking",
            origin="CNSHA",
            destination="NLRTM",
            equipment=[FURNITURE],
            routing_reference=choice["routing_reference"],
        )
        assert "covered by grant" in plan["authorization"]
        outcome = await tools("apply_plan", plan_id=plan["plan_id"])
        assert outcome["status"] == "applied"
        assert outcome["authorized_by"] == "grant:test-grant"

        booking = await tools("get_booking", reference=outcome["booking"]["reference"])
        assert booking["status"] == "CONFIRMED"
        assert booking["equipment"] == [
            {
                "type": "45G1",
                "units": 2,
                "commodity": "Flat-packed furniture",
                "cargo_weight_kg_per_container": 18000.0,
                "cargo_weight_kg_total": 36000.0,
            },
        ]
        assert set(booking["allowed_actions"]) == {"amend", "cancel_confirmed"}
        assert booking["transport_plan"][0]["vessel"] == choice["legs"][0]["vessel"]
        assert (await tools("list_bookings"))["bookings"][0]["reference"] == outcome["booking"]["reference"]
    assert len(sim.desk.bookings) == 1


async def test_answer_a_carrier_request_for_missing_cargo_weight(make_lashing: Factory) -> None:
    service = make_lashing(grant("create", "update"))
    async with connect(service) as tools:
        unweighed = {k: v for k, v in FURNITURE.items() if k != "cargo_weight_kg_per_container"}
        plan = await tools("propose_booking", origin="CNSHA", destination="NLRTM", equipment=[unweighed])
        request = (await tools("apply_plan", plan_id=plan["plan_id"]))["booking"]["request_reference"]

        pending = await tools("get_booking", reference=request)
        assert pending["status"] == "PENDING_UPDATE"
        assert pending["allowed_actions"] == {"update": "propose_change", "cancel_request": "propose_cancellation"}
        assert "Cargo gross weight" in pending["carrier_says"][0]["message"]

        fix = await tools("propose_change", reference=request, equipment=[FURNITURE])
        assert [c["field"] for c in fix["changes"]] == ["requestedEquipments"]
        assert (await tools("apply_plan", plan_id=fix["plan_id"]))["status"] == "applied"
        assert (await tools("get_booking", reference=request))["status"] == "CONFIRMED"


async def test_move_a_delayed_booking_to_another_sailing_with_a_persons_approval(
    make_lashing: Factory,
    sim: Simulator,
) -> None:
    reference = confirmed_at_carrier(sim)
    voyage = sim.desk.find(reference).route.legs[0].voyage  # type: ignore[union-attr]
    sim.delay(voyage.id, "SGSIN", hours=120, reason="Berth congestion at Singapore")
    person = Person("approve")
    async with connect(make_lashing(), person) as tools:
        tracked = await tools("track_shipment", reference=reference)
        assert tracked["final_arrival"]["basis"] == "estimated"
        assert any(call.get("delay_hours") == 120.0 for call in tracked["vessel_calls"])
        assert tracked["carrier_says"] == [{"about": "transport", "message": "Berth congestion at Singapore"}]

        options = (await tools("find_sailings", origin="CNSHA", destination="NLRTM"))["sailings"]
        faster = next(s for s in options if s["arrives"]["time"] < tracked["final_arrival"]["time"])
        plan = await tools("propose_change", reference=reference, routing_reference=faster["routing_reference"])
        assert plan["action"] == "amend"
        outcome = await tools("apply_plan", plan_id=plan["plan_id"])

    assert outcome["status"] == "applied"
    assert outcome["authorized_by"] == "approved:client"
    assert person.asked
    assert "Action: amend confirmed booking" in person.asked[0]
    assert f'Change: set routingReference to "{faster["routing_reference"]}"' in person.asked[0]
    booking = sim.desk.find(reference)
    assert booking.amendment is not None and booking.amendment.value == "AMENDMENT_CONFIRMED"
    assert booking.route is not None and booking.route.reference == faster["routing_reference"]


async def test_cancel_a_confirmed_booking(make_lashing: Factory, sim: Simulator) -> None:
    reference = confirmed_at_carrier(sim)
    async with connect(make_lashing(grant("cancel", bookings=("LSIM*",)))) as tools:
        plan = await tools("propose_cancellation", reference=reference, reason="Order withdrawn by the buyer")
        assert plan["action"] == "cancel_confirmed"
        assert (await tools("apply_plan", plan_id=plan["plan_id"]))["status"] == "applied"
        booking = await tools("get_booking", reference=reference)
    assert booking["status"] == "CANCELLED"
    assert booking["cancellation_status"] == "CANCELLATION_CONFIRMED"


async def test_mistakes_come_back_as_guidance(make_lashing: Factory, sim: Simulator) -> None:
    async with connect(make_lashing()) as tools:
        with pytest.raises(ToolFailed, match="not an ISO 6346 size-type code"):
            await tools(
                "propose_booking", origin="CNSHA", destination="NLRTM", equipment=[{"type": "big box", "units": 1}]
            )
        with pytest.raises(ToolFailed, match="UN/LOCODE"):
            await tools("find_sailings", origin="Shanghai", destination="NLRTM")
        with pytest.raises(ToolFailed, match="no booking"):
            await tools("get_booking", reference="LSIM999999")
        reference = confirmed_at_carrier(sim)
        with pytest.raises(ToolFailed, match="no pending amendment"):
            await tools("propose_cancellation", reference=reference, reason="x", amendment_only=True)
        with pytest.raises(ToolFailed, match="say what to change"):
            await tools("propose_change", reference=reference)


async def test_plans_can_be_listed_and_discarded(make_lashing: Factory) -> None:
    async with connect(make_lashing()) as tools:
        plan = await tools("propose_booking", origin="CNSHA", destination="NLRTM", equipment=[FURNITURE])
        assert [p["plan_id"] for p in (await tools("list_plans"))["plans"]] == [plan["plan_id"]]
        await tools("discard_plan", plan_id=plan["plan_id"])
        assert (await tools("list_plans"))["plans"] == []
        refused = await tools("apply_plan", plan_id=plan["plan_id"])
    assert refused["status"] == "refused"


async def test_weights_are_per_container_for_agents_and_line_totals_for_dcsa(
    make_lashing: Factory,
    sim: Simulator,
) -> None:
    reference = confirmed_at_carrier(sim)  # 1 x 22GP, 3,000 kg in DCSA's line total
    async with connect(make_lashing(grant("amend"))) as tools:
        booking = await tools("get_booking", reference=reference)
        assert booking["equipment"][0]["cargo_weight_kg_per_container"] == 3000.0
        plan = await tools("propose_change", reference=reference, equipment=[{"type": "22GP", "units": 3}])
        assert (await tools("apply_plan", plan_id=plan["plan_id"]))["status"] == "applied"
    line = sim.desk.find(reference).request["requestedEquipments"][0]
    assert line["units"] == 3
    assert line["commodities"][0]["cargoGrossWeight"] == {"value": 9000.0, "unit": "KGM"}  # 3,000 kg each


def test_pounds_are_converted() -> None:
    from lashing.views import weight_per_container  # noqa: PLC0415

    line = {"units": 2, "commodities": [{"cargoGrossWeight": {"value": 44092.452, "unit": "LBR"}}]}
    assert weight_per_container(line) == pytest.approx(10000.0, abs=0.01)


def test_passed_cut_offs_are_flagged() -> None:
    """Found by the model evals: an agent noticed a sailing whose documentation cut-off had passed."""
    import datetime as dt  # noqa: PLC0415

    from lashing.views import sailing  # noqa: PLC0415

    place = {"location": {"UNLocationCode": "SGSIN"}, "dateTime": "2026-09-23T10:00:00+08:00"}
    route = {
        "placeOfReceipt": place,
        "placeOfDelivery": place,
        "legs": [],
        "cutOffTimes": [
            {"cutOffDateTimeCode": "DCO", "cutOffDateTime": "2026-09-21T10:00:00+08:00"},
            {"cutOffDateTimeCode": "FCO", "cutOffDateTime": "2026-09-22T10:00:00+08:00"},
        ],
    }
    view = sailing(route, dt.datetime(2026, 9, 21, 8, 0, tzinfo=dt.UTC))
    assert view["cut_offs_passed"] == ["documentation"]
    assert "warning" in view
    assert "cut_offs_passed" not in sailing(route, dt.datetime(2026, 9, 20, tzinfo=dt.UTC))


async def test_the_simulator_offers_only_sailings_whose_cut_offs_are_ahead(sim: Simulator) -> None:
    for route in sim.world.routes("SGSIN", "AEJEA", sim.now, sim.now + dt.timedelta(days=21)):
        offered = route.reference in {r["routingReference"] for r in sim.point_to_point("SGSIN", "AEJEA")}
        assert offered == route.bookable(sim.now)
