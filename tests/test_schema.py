from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lashing.dcsa.schema import SchemaViolation, Spec, check, issues, load_spec

FIXTURES = Path(__file__).parent / "fixtures" / "dcsa"


def _component(schema: dict[str, Any]) -> tuple[str, bool] | None:
    """The component a media-type schema points at, and whether the payload is an array of it."""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1], False
    if schema.get("type") == "array" and "$ref" in schema.get("items", {}):
        return schema["items"]["$ref"].rsplit("/", 1)[-1], True
    return None


def _examples(spec: Spec) -> Iterator[Any]:
    """Every example DCSA embeds in a request or response body, with the component it illustrates."""
    document = load_spec(spec)
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            bodies = [("request", operation.get("requestBody", {}))]
            bodies += [(f"response {code}", response) for code, response in operation.get("responses", {}).items()]
            for where, body in bodies:
                for media in (body.get("content") or {}).values():
                    target = _component(media.get("schema", {}))
                    if target is None:
                        continue
                    for name, example in (media.get("examples") or {}).items():
                        if "value" in example:
                            label = f"{spec.value} {method.upper()} {path} {where} {name}"
                            yield pytest.param(spec, target, example["value"], id=label)


@pytest.mark.parametrize(("spec", "target", "payload"), [p for s in Spec for p in _examples(s)])
def test_dcsa_examples_conform_to_their_own_schemas(spec: Spec, target: tuple[str, bool], payload: Any) -> None:
    component, is_array = target
    for item in payload if is_array else [payload]:
        assert issues(spec, component, item) == []


def test_the_example_sweep_is_not_vacuous() -> None:
    # T&T 3.0.0 embeds no body examples; the Conformance Framework's responses stand in (below).
    assert all(any(True for _ in _examples(spec)) for spec in (Spec.BOOKING, Spec.SCHEDULES))


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("tnt-*.json")), ids=lambda p: p.stem)
def test_conformance_tracking_responses_validate(fixture: Path) -> None:
    check(Spec.TRACKING, "GetEventsResponse", json.loads(fixture.read_text()))


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("booking-*.json")), ids=lambda p: p.stem)
def test_conformance_sample_bookings_validate_against_2_0_5(fixture: Path) -> None:
    """The Conformance Framework's 2.0.0 sample messages must still be valid 2.0.5 requests."""
    check(Spec.BOOKING, "CreateBooking", json.loads(fixture.read_text()))


def test_violations_name_the_json_path() -> None:
    payload = json.loads((FIXTURES / "booking-dry-cargo.json").read_text())
    payload["requestedEquipments"][0]["units"] = "two"
    del payload["shipmentLocations"]
    with pytest.raises(SchemaViolation) as caught:
        check(Spec.BOOKING, "CreateBooking", payload)
    paths = {issue.path for issue in caught.value.issues}
    assert "$.requestedEquipments[0].units" in paths
    assert "$" in paths  # the missing required property is reported at the root


@pytest.mark.parametrize(
    "payload",
    [
        {"bookingStatus": "CANCELLED"},
        {"amendedBookingStatus": "AMENDMENT_CANCELLED", "reason": "wrong equipment"},
        {"bookingCancellationStatus": "CANCELLATION_RECEIVED", "reason": "order withdrawn"},
    ],
)
def test_each_cancellation_shape_is_accepted(payload: dict[str, str]) -> None:
    check(Spec.BOOKING, "CancelBookingRequest", payload)


@pytest.mark.parametrize("payload", [{}, {"reason": "no target"}])
def test_a_cancellation_must_say_what_it_cancels(payload: dict[str, str]) -> None:
    assert issues(Spec.BOOKING, "CancelBookingRequest", payload)


@pytest.mark.parametrize(
    ("value", "valid"),
    [("2026-09-18", True), ("2026-9-18", False), ("2026-02-30", False)],
)
def test_dates_are_checked(value: str, valid: bool) -> None:
    payload = json.loads((FIXTURES / "booking-dry-cargo.json").read_text())
    payload["expectedDepartureDate"] = value
    assert (issues(Spec.BOOKING, "CreateBooking", payload) == []) is valid


def test_unknown_component_is_an_error() -> None:
    with pytest.raises(KeyError):
        issues(Spec.BOOKING, "NoSuchThing", {})
