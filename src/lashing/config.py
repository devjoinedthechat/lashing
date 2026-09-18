"""Configuration: which carrier to talk to, who the shipper is, and what the agent may do alone.

Everything that decides authority lives here, in a file the agent's tools cannot write. See
`lashing.example.toml` for an annotated example.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lashing.carrier import Endpoints, Validation

ACTIONS = frozenset({"create", "update", "amend", "cancel"})


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Shipper:
    """Party details the agent must not invent: they come from the operator, not the model."""

    booking_agent: str
    contact_name: str
    contact_email: str | None = None
    contact_phone: str | None = None  # international format, e.g. +45 70262970
    shipper: str | None = None
    service_contract: str | None = None
    freight_payment: str | None = None  # PRE or COL

    def __post_init__(self) -> None:
        if not (self.contact_email or self.contact_phone):
            raise ConfigError("[shipper] needs contact_email or contact_phone: DCSA requires one for every contact")

    def document_parties(self) -> dict[str, Any]:
        detail: dict[str, str] = {"name": self.contact_name}
        if self.contact_email:
            detail["email"] = self.contact_email
        if self.contact_phone:
            detail["phone"] = self.contact_phone
        contact = [detail]
        parties: dict[str, Any] = {"bookingAgent": {"partyName": self.booking_agent, "partyContactDetails": contact}}
        if self.shipper:
            parties["shipper"] = {"partyName": self.shipper, "partyContactDetails": contact}
        return parties


@dataclass(frozen=True)
class Grant:
    """Standing permission for the agent to apply some writes without asking a person."""

    id: str
    actions: frozenset[str]
    bookings: tuple[str, ...] = ("*",)
    lanes: tuple[str, ...] = ("*",)
    fields: frozenset[str] | None = None
    max_units: int | None = None
    expires: dt.date | None = None
    note: str = ""

    def covers(self, scope: Scope, today: dt.date) -> bool:
        if self.expires is not None and today > self.expires:
            return False
        if scope.action not in self.actions:
            return False
        if scope.references and not any(
            fnmatch.fnmatchcase(ref, pattern) for ref in scope.references for pattern in self.bookings
        ):
            return False
        if scope.lane and not any(fnmatch.fnmatchcase(scope.lane, pattern) for pattern in self.lanes):
            return False
        if self.fields is not None and not scope.fields <= self.fields:
            return False
        return self.max_units is None or scope.units <= self.max_units


@dataclass(frozen=True)
class Scope:
    """The facts about a plan that authorization is decided on. Nothing here comes from carrier text."""

    action: str
    references: tuple[str, ...] = ()
    lane: str | None = None
    fields: frozenset[str] = frozenset()
    units: int = 0


@dataclass(frozen=True)
class Approvals:
    client: bool = True  # accept a yes from the MCP client's approval prompt (elicitation)
    operator: bool = True  # accept `lashing approve <plan>` from a terminal


@dataclass(frozen=True)
class Config:
    endpoints: Endpoints | None
    shipper: Shipper
    headers: dict[str, str] = field(default_factory=dict)
    validation: Validation = "warn"
    grants: tuple[Grant, ...] = ()
    approvals: Approvals = Approvals()
    state_dir: Path = Path(".lashing")
    plan_ttl: dt.timedelta = dt.timedelta(minutes=30)

    def grant_for(self, scope: Scope, today: dt.date) -> Grant | None:
        return next((g for g in self.grants if g.covers(scope, today)), None)


DEMO_SHIPPER = Shipper(
    booking_agent="Demo Forwarding ApS",
    contact_name="Operations desk",
    contact_email="operations@forwarding.example",
    shipper="Demo Furniture A/S",
    service_contract="DEMO-SC-2026",
    freight_payment="PRE",
)


def _grant(raw: dict[str, Any], n: int) -> Grant:
    actions = frozenset(raw.get("actions", []))
    if not actions or not actions <= ACTIONS:
        raise ConfigError(f"grant {n}: actions must be a non-empty subset of {sorted(ACTIONS)}")
    expires = raw.get("expires")
    if isinstance(expires, str):
        expires = dt.date.fromisoformat(expires)
    if expires is not None and not isinstance(expires, dt.date):
        raise ConfigError(f"grant {n}: expires must be a date")
    fields = raw.get("fields")
    return Grant(
        id=str(raw.get("id", f"grant-{n}")),
        actions=actions,
        bookings=tuple(raw.get("bookings", ["*"])),
        lanes=tuple(raw.get("lanes", ["*"])),
        fields=frozenset(fields) if fields is not None else None,
        max_units=raw.get("max_units"),
        expires=expires,
        note=str(raw.get("note", "")),
    )


def _endpoints(raw: dict[str, Any]) -> Endpoints | None:
    if not raw:
        return None
    if "base_url" in raw:
        return Endpoints.under(raw["base_url"])
    try:
        return Endpoints(booking=raw["booking_url"], schedules=raw["schedules_url"], tracking=raw["tracking_url"])
    except KeyError as missing:
        raise ConfigError(
            f"[carrier] needs base_url, or all of booking_url, schedules_url, tracking_url ({missing})"
        ) from None


def _headers(raw: dict[str, Any]) -> dict[str, str]:
    """Credentials are read from the environment, never from the file."""
    headers: dict[str, str] = {}
    if env := raw.get("auth_env"):
        token = os.environ.get(env)
        if not token:
            raise ConfigError(f"[carrier] auth_env names {env}, which is not set")
        headers[raw.get("auth_header", "Authorization")] = raw.get("auth_prefix", "Bearer ") + token
    return headers


def load(path: Path | None) -> Config:
    """Load a config file; with no file, run against the built-in simulator with no grants."""
    if path is None:
        return Config(endpoints=None, shipper=DEMO_SHIPPER)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    carrier = raw.get("carrier", {})
    shipper_raw = raw.get("shipper")
    try:
        shipper = Shipper(**shipper_raw) if shipper_raw else DEMO_SHIPPER
    except TypeError as error:
        raise ConfigError(f"[shipper]: {error}") from None
    approvals_raw = raw.get("approvals", {})
    state_dir = Path(raw.get("state_dir", ".lashing"))
    if not state_dir.is_absolute():
        state_dir = path.parent / state_dir
    validation = carrier.get("validation", "warn")
    if validation not in ("strict", "warn", "off"):
        raise ConfigError("[carrier] validation must be strict, warn or off")
    return Config(
        endpoints=_endpoints(carrier),
        shipper=shipper,
        headers=_headers(carrier),
        validation=validation,
        grants=tuple(_grant(g, n) for n, g in enumerate(raw.get("grant", []), start=1)),
        approvals=Approvals(
            client=bool(approvals_raw.get("client", True)), operator=bool(approvals_raw.get("operator", True))
        ),
        state_dir=state_dir,
        plan_ttl=dt.timedelta(minutes=int(raw.get("plan_ttl_minutes", 30))),
    )
