from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lashing.dcsa.booking import (
    AFTER_CONFIRMATION,
    BEFORE_CONFIRMATION,
    TERMINAL,
    AmendmentStatus,
    BookingState,
    BookingStatus,
    Cancellation,
    CancellationStatus,
    Change,
    LifecycleError,
    cancellation_kind,
    cancellation_payload,
)
from lashing.dcsa.schema import Spec, issues

states = st.builds(
    BookingState,
    status=st.sampled_from(BookingStatus),
    amendment=st.none() | st.sampled_from(AmendmentStatus),
    cancellation=st.none() | st.sampled_from(CancellationStatus),
    request_reference=st.just("cbrr-1"),
    booking_reference=st.none() | st.just("CBR1"),
)


@pytest.mark.parametrize("status", sorted(BEFORE_CONFIRMATION))
def test_before_confirmation_a_put_is_an_update_and_cancel_uses_the_request_reference(status: BookingStatus) -> None:
    state = BookingState(status, request_reference="cbrr-1")
    assert state.change() is Change.UPDATE
    assert state.require_cancellation() is Cancellation.REQUEST
    assert state.path_reference(Cancellation.REQUEST) == "cbrr-1"


@pytest.mark.parametrize("status", sorted(AFTER_CONFIRMATION))
def test_after_confirmation_a_put_is_an_amendment_on_the_booking_reference(status: BookingStatus) -> None:
    state = BookingState(status, request_reference="cbrr-1", booking_reference="CBR1")
    assert state.change() is Change.AMEND
    assert state.path_reference(Change.AMEND) == "CBR1"
    assert state.require_cancellation() is Cancellation.CONFIRMED


@pytest.mark.parametrize("status", sorted(TERMINAL))
def test_nothing_is_allowed_in_a_terminal_state(status: BookingStatus) -> None:
    state = BookingState(status, request_reference="cbrr-1", booking_reference="CBR1")
    assert state.allowed_actions() == []
    with pytest.raises(LifecycleError, match="cannot be changed"):
        state.require_change()
    with pytest.raises(LifecycleError, match="cannot be cancelled"):
        state.require_cancellation()


def test_a_pending_cancellation_blocks_amendments_and_a_second_cancellation() -> None:
    state = BookingState(
        BookingStatus.CONFIRMED,
        cancellation=CancellationStatus.CANCELLATION_RECEIVED,
        request_reference="cbrr-1",
        booking_reference="CBR1",
    )
    assert state.change() is None
    assert Cancellation.CONFIRMED not in state.cancellations()


def test_only_a_received_amendment_can_be_cancelled_on_its_own() -> None:
    pending = BookingState(
        BookingStatus.CONFIRMED,
        amendment=AmendmentStatus.AMENDMENT_RECEIVED,
        request_reference="cbrr-1",
        booking_reference="CBR1",
    )
    assert pending.require_cancellation(amendment_only=True) is Cancellation.AMENDMENT
    settled = BookingState(BookingStatus.CONFIRMED, amendment=AmendmentStatus.AMENDMENT_CONFIRMED)
    with pytest.raises(LifecycleError, match="no pending amendment"):
        settled.require_cancellation(amendment_only=True)


@given(states)
def test_every_allowed_cancellation_produces_a_valid_patch_body(state: BookingState) -> None:
    for kind in state.cancellations():
        body = cancellation_payload(kind, reason="customer withdrew the order")
        assert issues(Spec.BOOKING, "CancelBookingRequest", body) == []
        assert cancellation_kind(body) is kind
        assert state.path_reference(kind)  # never raises for an allowed action


@given(states)
def test_allowed_actions_are_consistent_with_the_requirements(state: BookingState) -> None:
    allowed = set(state.allowed_actions())
    assert (Change.UPDATE.value in allowed or Change.AMEND.value in allowed) == (state.change() is not None)
    if state.status in TERMINAL:
        assert allowed == set()


@pytest.mark.parametrize(
    "body",
    [
        {"bookingStatus": "CONFIRMED"},
        {"bookingStatus": "CANCELLED", "bookingCancellationStatus": "CANCELLATION_RECEIVED"},
        {"reason": "no target"},
    ],
)
def test_patch_bodies_the_schema_cannot_catch_are_refused(body: dict[str, str]) -> None:
    with pytest.raises(LifecycleError):
        cancellation_kind(body)


def test_state_is_read_from_a_booking_payload() -> None:
    state = BookingState.from_payload(
        {
            "bookingStatus": "CONFIRMED",
            "amendedBookingStatus": "AMENDMENT_RECEIVED",
            "carrierBookingRequestReference": "cbrr-1",
            "carrierBookingReference": "CBR1",
        },
    )
    assert state.reference == "CBR1"
    assert state.allowed_actions() == ["amend", "cancel_confirmed", "cancel_amendment"]
