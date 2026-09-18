"""The DCSA Booking 2.0 lifecycle, seen from the shipper's side.

The standard spreads its rules over prose in the OpenAPI descriptions: which states allow an update
versus an amendment, which of three PATCH shapes cancels what, and which reference the path must
carry. They are encoded once here, so neither the MCP tools nor an agent has to get them right.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class BookingStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PENDING_UPDATE = "PENDING_UPDATE"
    UPDATE_RECEIVED = "UPDATE_RECEIVED"
    CONFIRMED = "CONFIRMED"
    PENDING_AMENDMENT = "PENDING_AMENDMENT"
    REJECTED = "REJECTED"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class AmendmentStatus(StrEnum):
    AMENDMENT_RECEIVED = "AMENDMENT_RECEIVED"
    AMENDMENT_CONFIRMED = "AMENDMENT_CONFIRMED"
    AMENDMENT_DECLINED = "AMENDMENT_DECLINED"
    AMENDMENT_CANCELLED = "AMENDMENT_CANCELLED"


class CancellationStatus(StrEnum):
    CANCELLATION_RECEIVED = "CANCELLATION_RECEIVED"
    CANCELLATION_DECLINED = "CANCELLATION_DECLINED"
    CANCELLATION_CONFIRMED = "CANCELLATION_CONFIRMED"


BEFORE_CONFIRMATION = frozenset(
    {BookingStatus.RECEIVED, BookingStatus.PENDING_UPDATE, BookingStatus.UPDATE_RECEIVED},
)
AFTER_CONFIRMATION = frozenset({BookingStatus.CONFIRMED, BookingStatus.PENDING_AMENDMENT})
TERMINAL = frozenset(
    {BookingStatus.REJECTED, BookingStatus.DECLINED, BookingStatus.CANCELLED, BookingStatus.COMPLETED},
)


class Change(StrEnum):
    """What a PUT means in the current state."""

    UPDATE = "update"  # UseCase 3: before confirmation
    AMEND = "amend"  # UseCase 7: after confirmation; co-exists with the confirmed booking until processed


class Cancellation(StrEnum):
    """The three things a PATCH can cancel, each with its own payload and path reference."""

    REQUEST = "cancel_request"  # UseCase 11: an unconfirmed booking request
    AMENDMENT = "cancel_amendment"  # UseCase 9: only the pending amendment
    CONFIRMED = "cancel_confirmed"  # UseCase 13: a confirmed booking; the carrier may decline


class LifecycleError(ValueError):
    """An action the standard does not allow in the booking's current state."""


@dataclass(frozen=True)
class BookingState:
    status: BookingStatus
    amendment: AmendmentStatus | None = None
    cancellation: CancellationStatus | None = None
    request_reference: str | None = None
    booking_reference: str | None = None

    @classmethod
    def from_payload(cls, booking: Mapping[str, Any]) -> BookingState:
        """Read the state from a Booking; a status the standard does not define is a LifecycleError."""
        try:
            status = BookingStatus(booking["bookingStatus"])
            amendment = booking.get("amendedBookingStatus")
            cancellation = booking.get("bookingCancellationStatus")
            return cls(
                status=status,
                amendment=AmendmentStatus(amendment) if amendment else None,
                cancellation=CancellationStatus(cancellation) if cancellation else None,
                request_reference=booking.get("carrierBookingRequestReference"),
                booking_reference=booking.get("carrierBookingReference"),
            )
        except KeyError:
            raise LifecycleError("the carrier's booking has no bookingStatus") from None
        except ValueError as error:
            raise LifecycleError(f"the carrier sent a status DCSA Booking 2.0 does not define: {error}") from None

    @property
    def reference(self) -> str:
        """The reference to show people: the booking reference once there is one."""
        reference = self.booking_reference or self.request_reference
        if reference is None:
            raise LifecycleError("booking carries neither carrierBookingReference nor carrierBookingRequestReference")
        return reference

    @property
    def label(self) -> str:
        return self.booking_reference or self.request_reference or "(unreferenced booking)"

    @property
    def cancellation_pending(self) -> bool:
        return self.cancellation is CancellationStatus.CANCELLATION_RECEIVED

    @property
    def _confirmed(self) -> bool:
        # A confirmed booking is addressed by its carrierBookingReference; one without it cannot be acted on.
        return self.status in AFTER_CONFIRMATION and self.booking_reference is not None

    def change(self) -> Change | None:
        if self.status in BEFORE_CONFIRMATION and self.request_reference:
            return Change.UPDATE
        if self._confirmed and not self.cancellation_pending:
            return Change.AMEND
        return None

    def cancellations(self) -> tuple[Cancellation, ...]:
        allowed: list[Cancellation] = []
        if self.status in BEFORE_CONFIRMATION and self.request_reference:
            allowed.append(Cancellation.REQUEST)
        if self._confirmed and not self.cancellation_pending:
            allowed.append(Cancellation.CONFIRMED)
        if self._confirmed and self.amendment is AmendmentStatus.AMENDMENT_RECEIVED:
            allowed.append(Cancellation.AMENDMENT)
        return tuple(allowed)

    def allowed_actions(self) -> list[str]:
        actions = []
        change = self.change()
        if change is not None:
            actions.append(change.value)
        actions.extend(c.value for c in self.cancellations())
        return actions

    def path_reference(self, action: Change | Cancellation) -> str:
        """The reference the request path must carry for this action."""
        if action is Cancellation.REQUEST:
            if not self.request_reference:
                raise LifecycleError("cancelling a booking request needs its carrierBookingRequestReference")
            return self.request_reference
        if action in (Cancellation.AMENDMENT, Cancellation.CONFIRMED, Change.AMEND):
            if not self.booking_reference:
                raise LifecycleError(f"{action.value} needs the carrierBookingReference of a confirmed booking")
            return self.booking_reference
        return self.reference

    def require_change(self) -> Change:
        change = self.change()
        if change is None:
            raise LifecycleError(self._refusal("changed"))
        return change

    def require_cancellation(self, *, amendment_only: bool = False) -> Cancellation:
        options = self.cancellations()
        if amendment_only:
            if Cancellation.AMENDMENT not in options:
                raise LifecycleError(f"booking {self.label} has no pending amendment to cancel")
            return Cancellation.AMENDMENT
        for kind in (Cancellation.REQUEST, Cancellation.CONFIRMED):
            if kind in options:
                return kind
        raise LifecycleError(self._refusal("cancelled"))

    def _refusal(self, verb: str) -> str:
        why = f"status {self.status.value}"
        if self.cancellation_pending:
            why += " with a cancellation awaiting the carrier"
        return f"booking {self.label} cannot be {verb} in {why}"


def cancellation_payload(kind: Cancellation, reason: str | None = None) -> dict[str, str]:
    field, value = {
        Cancellation.REQUEST: ("bookingStatus", BookingStatus.CANCELLED.value),
        Cancellation.AMENDMENT: ("amendedBookingStatus", AmendmentStatus.AMENDMENT_CANCELLED.value),
        Cancellation.CONFIRMED: ("bookingCancellationStatus", CancellationStatus.CANCELLATION_RECEIVED.value),
    }[kind]
    payload = {field: value}
    if reason:
        payload["reason"] = reason
    return payload


def cancellation_kind(payload: Mapping[str, Any]) -> Cancellation:
    """Which cancellation a PATCH body asks for, enforcing the single value each shape allows."""
    shapes = {
        "bookingStatus": (Cancellation.REQUEST, BookingStatus.CANCELLED.value),
        "amendedBookingStatus": (Cancellation.AMENDMENT, AmendmentStatus.AMENDMENT_CANCELLED.value),
        "bookingCancellationStatus": (Cancellation.CONFIRMED, CancellationStatus.CANCELLATION_RECEIVED.value),
    }
    present = [field for field in shapes if field in payload]
    if len(present) != 1:
        raise LifecycleError("a cancellation sets exactly one of " + ", ".join(shapes))
    kind, required = shapes[present[0]]
    if payload[present[0]] != required:
        raise LifecycleError(f"{present[0]} can only be set to {required}")
    return kind
