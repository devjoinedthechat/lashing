"""Plans: a write the agent has proposed, which lashing has validated and may later apply once.

A plan is the exact DCSA request body plus the facts authorization is decided on. Plans and
everything that happens to them are recorded in the ledger, which is how the `lashing approve`
command (a separate process) and the MCP server share them.
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

from lashing.config import Scope
from lashing.ledger import Ledger

# What a plan does at the carrier, and the grant action that covers it.
KINDS = {
    "create": "create",
    "update": "update",
    "amend": "amend",
    "cancel_request": "cancel",
    "cancel_confirmed": "cancel",
    "cancel_amendment": "cancel",
}
FINAL = {"applied", "refused", "discarded", "failed", "stale", "expired"}


@dataclass(frozen=True)
class Change:
    field: str
    before: Any
    after: Any


@dataclass(frozen=True)
class Plan:
    id: str
    kind: str
    summary: str
    payload: dict[str, Any]
    scope: Scope
    created_at: dt.datetime
    expires_at: dt.datetime
    reference: str | None = None  # the path reference the request will use
    fingerprint: str | None = None  # of the booking as it was when planned
    changes: tuple[Change, ...] = field(default_factory=tuple)

    @staticmethod
    def new_id() -> str:
        return "pln_" + secrets.token_urlsafe(9)

    def record(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"]["fields"] = sorted(self.scope.fields)
        data["created_at"] = self.created_at.isoformat()
        data["expires_at"] = self.expires_at.isoformat()
        return data

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> Plan:
        scope = data["scope"]
        return cls(
            id=data["id"],
            kind=data["kind"],
            summary=data["summary"],
            payload=data["payload"],
            scope=Scope(
                action=scope["action"],
                references=tuple(scope["references"]),
                lane=scope["lane"],
                fields=frozenset(scope["fields"]),
                units=scope["units"],
            ),
            created_at=dt.datetime.fromisoformat(data["created_at"]),
            expires_at=dt.datetime.fromisoformat(data["expires_at"]),
            reference=data.get("reference"),
            fingerprint=data.get("fingerprint"),
            changes=tuple(Change(**c) for c in data.get("changes", ())),
        )

    def view(self) -> dict[str, Any]:
        """What an agent or operator needs to see about a plan."""
        out: dict[str, Any] = {
            "plan_id": self.id,
            "action": self.kind,
            "summary": self.summary,
            "expires_at": self.expires_at.isoformat(timespec="seconds"),
        }
        if self.reference:
            out["booking"] = self.reference
        if self.changes:
            out["changes"] = [{"field": c.field, "from": c.before, "to": c.after} for c in self.changes]
        return out


@dataclass
class PlanState:
    plan: Plan
    status: str = "proposed"
    approved_by: str | None = None
    outcome: dict[str, Any] | None = None


class PlanBook:
    """Plans as the ledger records them."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def propose(self, plan: Plan) -> None:
        self.ledger.append("proposed", plan=plan.record())

    def states(self) -> dict[str, PlanState]:
        found: dict[str, PlanState] = {}
        for entry in self.ledger.entries():
            kind = entry["kind"]
            if kind == "proposed":
                plan = Plan.from_record(entry["plan"])
                found[plan.id] = PlanState(plan)
                continue
            state = found.get(entry.get("plan_id", ""))
            if state is None:
                continue
            if kind == "approved":
                state.approved_by = entry.get("by", "operator")
            elif kind == "applying":
                state.status = "applying"
            elif kind in FINAL:
                state.status = kind
                state.outcome = entry
        return found

    def get(self, plan_id: str) -> PlanState | None:
        return self.states().get(plan_id)

    def open(self) -> Iterator[PlanState]:
        return (s for s in self.states().values() if s.status == "proposed")

    def close(self, plan_id: str, status: str, **details: Any) -> dict[str, Any]:
        if status not in FINAL:
            raise ValueError(f"{status!r} is not a final plan status")
        return self.ledger.append(status, plan_id=plan_id, **details)

    def approve(self, plan_id: str, by: str) -> dict[str, Any]:
        return self.ledger.append("approved", plan_id=plan_id, by=by)

    def claim(self, plan_id: str, authorized_by: str) -> bool:
        """Mark a plan as being applied, unless anyone (in any process) already has or has closed it."""

        def untouched(entries: list[dict[str, Any]]) -> bool:
            return not any(
                e.get("plan_id") == plan_id and (e["kind"] == "applying" or e["kind"] in FINAL) for e in entries
            )

        return self.ledger.append_if(untouched, "applying", plan_id=plan_id, authorized_by=authorized_by) is not None
