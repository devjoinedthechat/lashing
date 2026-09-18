"""Validation against the vendored DCSA OpenAPI specs.

Every payload lashing sends to a carrier, and every payload the simulator returns, is checked here
against the published standard, so "conforms to DCSA" is a tested property rather than a claim.
OpenAPI 3.0 schemas are a dialect of JSON Schema draft 4, which is what we validate with.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft4Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT4


class Spec(StrEnum):
    BOOKING = "bkg-2.0.5"
    SCHEDULES = "cs-1.0.4"
    TRACKING = "tnt-3.0.0"


@dataclass(frozen=True)
class SchemaIssue:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class SchemaViolation(ValueError):
    def __init__(self, spec: Spec, component: str, issues: list[SchemaIssue]) -> None:
        self.spec = spec
        self.component = component
        self.issues = issues
        listed = "; ".join(str(issue) for issue in issues[:5])
        more = f" (+{len(issues) - 5} more)" if len(issues) > 5 else ""
        super().__init__(f"payload does not conform to {spec.value} {component}: {listed}{more}")


_FORMATS = FormatChecker(formats=())


@_FORMATS.checks("date", raises=ValueError)
def _is_date(value: object) -> bool:
    if isinstance(value, str):
        if len(value) != 10:  # fromisoformat also takes the basic form 20260918, which DCSA does not
            raise ValueError("expected YYYY-MM-DD")
        dt.date.fromisoformat(value)
    return True


@_FORMATS.checks("date-time", raises=ValueError)
def _is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("date-time needs a UTC offset (RFC 3339)")
    return True


@cache
def load_spec(spec: Spec) -> dict[str, Any]:
    text = resources.files("lashing.dcsa.specs").joinpath(f"{spec.value}.json").read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(text)
    return document


def _uri(spec: Spec) -> str:
    return f"urn:dcsa:{spec.value}"


@cache
def _registry() -> Registry[Any]:
    registry: Registry[Any] = Registry()
    for spec in Spec:
        resource = Resource.from_contents(load_spec(spec), default_specification=DRAFT4)
        registry = registry.with_resource(_uri(spec), resource)
    return registry


@cache
def validator(spec: Spec, component: str) -> Draft4Validator:
    if component not in load_spec(spec)["components"]["schemas"]:
        raise KeyError(f"{spec.value} has no schema component {component!r}")
    schema = {"$ref": f"{_uri(spec)}#/components/schemas/{component}"}
    return Draft4Validator(schema, registry=_registry(), format_checker=_FORMATS)


def _path(absolute_path: Any) -> str:
    out = "$"
    for part in absolute_path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _message(error: Any) -> str:
    if error.validator == "oneOf":
        return "matches none (or more than one) of the allowed shapes"
    message = str(error.message)
    return message if len(message) <= 200 else message[:197] + "..."


def issues(spec: Spec, component: str, payload: Any) -> list[SchemaIssue]:
    errors = sorted(validator(spec, component).iter_errors(payload), key=lambda e: list(e.absolute_path))
    return [SchemaIssue(_path(error.absolute_path), _message(error)) for error in errors]


def check(spec: Spec, component: str, payload: Any) -> None:
    found = issues(spec, component, payload)
    if found:
        raise SchemaViolation(spec, component, found)
