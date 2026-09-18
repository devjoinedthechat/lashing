"""Configuration: which carrier to talk to, who the shipper is, and what the agent may do alone.

Everything that decides authority lives here, in a file the agent's tools cannot write. Because a
typo in a grant could silently widen it, the file is read strictly: unknown keys, wrong types and
out-of-range values are errors, never defaults. See `lashing.example.toml`.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import os
import tomllib
from collections.abc import Mapping
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
        if self.freight_payment not in (None, "PRE", "COL"):
            raise ConfigError("[shipper] freight_payment must be PRE or COL")

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
class Scope:
    """The facts about a plan that authorization is decided on.

    lashing computes them from the request it built and, for changes, from the booking as the carrier
    holds it (its references, lane and units); never from free text anyone wrote.
    """

    action: str
    references: tuple[str, ...] = ()
    lane: str | None = None
    fields: frozenset[str] = frozenset()
    units: int = 0


@dataclass(frozen=True)
class Grant:
    """Standing permission for the agent to apply some writes without asking a person."""

    id: str
    actions: frozenset[str]
    bookings: tuple[str, ...] = ("*",)
    lanes: tuple[str, ...] = ("*",)
    fields: frozenset[str] | None = None
    max_units: int | None = None
    max_per_day: int | None = None
    expires: dt.date | None = None
    note: str = ""

    def covers(self, scope: Scope, today: dt.date) -> bool:
        if self.expires is not None and today > self.expires:
            return False
        if scope.action not in self.actions:
            return False
        if self.bookings != ("*",) and not (
            scope.references  # a booking-restricted grant needs a booking it can check
            and any(fnmatch.fnmatchcase(ref, pattern) for ref in scope.references for pattern in self.bookings)
        ):
            return False
        if self.lanes != ("*",) and not (
            scope.lane is not None  # likewise a lane-restricted grant needs a known lane
            and any(fnmatch.fnmatchcase(scope.lane, pattern) for pattern in self.lanes)
        ):
            return False
        if self.fields is not None and not scope.fields <= self.fields:
            return False
        return self.max_units is None or scope.units <= self.max_units


@dataclass(frozen=True)
class Approvals:
    client: bool = True  # accept a yes from the MCP client's approval prompt (elicitation)
    operator: bool = True  # accept `lashing approve <plan>` from a terminal


@dataclass(frozen=True)
class Config:
    endpoints: Endpoints | None
    shipper: Shipper
    headers: dict[str, str] = field(default_factory=dict, repr=False)  # carries credentials
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


# -- strict reading --------------------------------------------------------------------------------


def _table(raw: Any, where: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be a table")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"{where} has unknown keys {unknown}; allowed: {sorted(allowed)}")
    return raw


def _str(raw: Mapping[str, Any], key: str, where: str, default: str | None = None) -> str | None:
    value = raw.get(key, default)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value


def _strings(raw: Mapping[str, Any], key: str, where: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f'{where}.{key} must be a non-empty list of strings, e.g. ["LSIM*"]')
    return tuple(value)


def _bool(raw: Mapping[str, Any], key: str, where: str, *, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} must be true or false")
    return value


def _count(raw: Mapping[str, Any], key: str, where: str, minimum: int) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{where}.{key} must be a whole number of at least {minimum}")
    return value


def _date(raw: Mapping[str, Any], key: str, where: str) -> dt.date | None:
    value = raw.get(key)
    if isinstance(value, str):
        try:
            value = dt.date.fromisoformat(value)
        except ValueError:
            raise ConfigError(f"{where}.{key} must be a date like 2026-12-31") from None
    if value is not None and (not isinstance(value, dt.date) or isinstance(value, dt.datetime)):
        raise ConfigError(f"{where}.{key} must be a date like 2026-12-31")
    return value


GRANT_KEYS = {"id", "actions", "bookings", "lanes", "fields", "max_units", "max_per_day", "expires", "note"}


def _grant(raw: Any, n: int) -> Grant:
    where = f"grant {n}"
    raw = _table(raw, where, GRANT_KEYS)
    actions = frozenset(_strings(raw, "actions", where, ()))
    if not actions or not actions <= ACTIONS:
        raise ConfigError(f"{where}.actions must be a non-empty subset of {sorted(ACTIONS)}")
    return Grant(
        id=_str(raw, "id", where, f"grant-{n}") or f"grant-{n}",
        actions=actions,
        bookings=_strings(raw, "bookings", where, ("*",)),
        lanes=_strings(raw, "lanes", where, ("*",)),
        fields=frozenset(_strings(raw, "fields", where, ())) if "fields" in raw else None,
        max_units=_count(raw, "max_units", where, 0),
        max_per_day=_count(raw, "max_per_day", where, 1),
        expires=_date(raw, "expires", where),
        note=_str(raw, "note", where) or "",
    )


def _endpoints(raw: Mapping[str, Any]) -> Endpoints | None:
    if not raw:
        return None
    if "base_url" in raw:
        return Endpoints.under(_str(raw, "base_url", "[carrier]") or "")
    try:
        return Endpoints(booking=raw["booking_url"], schedules=raw["schedules_url"], tracking=raw["tracking_url"])
    except KeyError as missing:
        raise ConfigError(
            f"[carrier] needs base_url, or all of booking_url, schedules_url, tracking_url ({missing})",
        ) from None


def _headers(raw: Mapping[str, Any]) -> dict[str, str]:
    """Credentials are read from the environment, never from the file."""
    headers: dict[str, str] = {}
    if env := _str(raw, "auth_env", "[carrier]"):
        token = os.environ.get(env)
        if not token:
            raise ConfigError(f"[carrier] auth_env names {env}, which is not set")
        header = _str(raw, "auth_header", "[carrier]", "Authorization") or "Authorization"
        prefix = raw.get("auth_prefix", "Bearer ")
        if not isinstance(prefix, str):
            raise ConfigError("[carrier].auth_prefix must be a string")
        headers[header] = prefix + token
    return headers


TOP_KEYS = {"state_dir", "plan_ttl_minutes", "carrier", "shipper", "approvals", "grant"}
CARRIER_KEYS = {
    "base_url", "booking_url", "schedules_url", "tracking_url",
    "auth_env", "auth_header", "auth_prefix", "validation",
}  # fmt: skip
SHIPPER_KEYS = {
    "booking_agent", "contact_name", "contact_email", "contact_phone", "shipper", "service_contract", "freight_payment",
}  # fmt: skip


def load(path: Path | None, *, credentials: bool = True) -> Config:
    """Load a config file; with no file, run against the built-in simulator with no grants.

    `credentials=False` skips reading the carrier's secret, for commands that never call the carrier.
    """
    if path is None:
        return Config(endpoints=None, shipper=DEMO_SHIPPER)
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    raw = _table(parsed, "the config", TOP_KEYS)
    carrier = _table(raw.get("carrier", {}), "[carrier]", CARRIER_KEYS)
    shipper_raw = _table(raw.get("shipper", {}), "[shipper]", SHIPPER_KEYS)
    approvals_raw = _table(raw.get("approvals", {}), "[approvals]", {"client", "operator"})
    grants_raw = raw.get("grant", [])
    if not isinstance(grants_raw, list):
        raise ConfigError("grants are written as [[grant]] tables")
    try:
        shipper = Shipper(**shipper_raw) if shipper_raw else DEMO_SHIPPER
    except TypeError as error:
        raise ConfigError(f"[shipper]: {error}") from None
    state_dir = Path(_str(raw, "state_dir", "the config", ".lashing") or ".lashing")
    if not state_dir.is_absolute():
        state_dir = path.parent / state_dir
    validation = carrier.get("validation", "warn")
    if validation not in ("strict", "warn", "off"):
        raise ConfigError("[carrier] validation must be strict, warn or off")
    return Config(
        endpoints=_endpoints(carrier),
        shipper=shipper,
        headers=_headers(carrier) if credentials else {},
        validation=validation,
        grants=tuple(_grant(g, n) for n, g in enumerate(grants_raw, start=1)),
        approvals=Approvals(
            client=_bool(approvals_raw, "client", "[approvals]", default=True),
            operator=_bool(approvals_raw, "operator", "[approvals]", default=True),
        ),
        state_dir=state_dir,
        plan_ttl=dt.timedelta(minutes=_count(raw, "plan_ttl_minutes", "the config", 1) or 30),
    )
