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
# "unknown": sent, but whether the carrier acted is not known. It is final so the plan is never
# resent blindly; an operator checks with the carrier and records what happened (`lashing resolve`).
FINAL = frozenset({"applied", "refused", "discarded", "failed", "stale", "expired", "unknown"})
DAY = dt.timedelta(days=1)


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
    amended_fingerprint: str | None = None  # of the pending amendment the plan was built on, if any
    changes: tuple[Change, ...] = field(default_factory=tuple)
    facts: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # what a person approving it is shown

    @staticmethod
    def new_id() -> str:
        return "pln_" + secrets.token_urlsafe(9)

    def record(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"]["fields"] = sorted(self.scope.fields)
        data["created_at"] = self.created_at.isoformat()
        data["expires_at"] = self.expires_at.isoformat()
        data["facts"] = [list(f) for f in self.facts]
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
            amended_fingerprint=data.get("amended_fingerprint"),
            changes=tuple(Change(**c) for c in data.get("changes", ())),
            facts=tuple((str(k), str(v)) for k, v in data.get("facts", ())),
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

    def question(self) -> str:
        """What a person is asked to approve: every fact, laid out by lashing, never the agent's prose."""
        lines = ["lashing asks you to approve this request to the carrier.", ""]
        lines += [f"{label}: {value}" for label, value in self.facts]
        lines += ["", f"Plan {self.id}. Text in quotation marks was written by the AI agent."]
        return "\n".join(lines)


@dataclass
class PlanState:
    plan: Plan
    status: str = "proposed"
    approved_by: str | None = None
    outcome: dict[str, Any] | None = None


def _latest(entries: list[dict[str, Any]], plan_id: str) -> dict[str, Any] | None:
    """The most recent entry about a plan, stopping at its proposal."""
    for entry in reversed(entries):
        if entry.get("plan_id") == plan_id:
            return entry
    return None


class PlanBook:
    """Plans as the ledger records them."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self._states: dict[str, PlanState] = {}
        self._seen = 0
        self._generation = -1

    def propose(self, plan: Plan) -> None:
        self.ledger.append("proposed", plan_id=plan.id, plan=plan.record())

    def states(self) -> dict[str, PlanState]:
        """Every plan and what has happened to it, updated from only the entries added since last time."""
        new, generation = self.ledger.since(self._seen)
        while generation != self._generation:  # the ledger was rebuilt (first read, or the file replaced)
            self._states, self._seen, self._generation = {}, 0, generation
            new, generation = self.ledger.since(0)
        for entry in new:
            self._apply(entry)
        self._seen += len(new)
        return self._states

    def _apply(self, entry: dict[str, Any]) -> None:
        kind = entry["kind"]
        if kind == "proposed":
            plan = Plan.from_record(entry["plan"])
            self._states[plan.id] = PlanState(plan)
            return
        state = self._states.get(entry.get("plan_id", ""))
        if state is None:
            return
        if kind == "approved":
            state.approved_by = entry.get("by", "operator")
        elif kind == "applying":
            state.status = "applying"
        elif kind == "released":  # the request never left; the plan can be applied again
            state.status = "proposed"
        elif kind == "resolved":  # an operator recorded what happened to a plan in doubt
            state.status = entry["outcome"]
            state.outcome = entry
        elif kind in FINAL:
            state.status = kind
            state.outcome = entry

    def get(self, plan_id: str) -> PlanState | None:
        return self.states().get(plan_id)

    def open(self, now: dt.datetime | None = None) -> Iterator[PlanState]:
        """Plans still waiting to be applied (and, given `now`, not yet expired)."""
        return (
            s for s in self.states().values() if s.status == "proposed" and (now is None or s.plan.expires_at >= now)
        )

    def in_doubt(self) -> Iterator[PlanState]:
        """Plans that may or may not have reached the carrier: being applied, or with an unknown outcome."""
        return (s for s in self.states().values() if s.status in ("applying", "unknown"))

    def close(self, plan_id: str, status: str, **details: Any) -> dict[str, Any] | None:
        """Record a plan's final outcome, unless it already has one: a plan never ends twice."""
        if status not in FINAL:
            raise ValueError(f"{status!r} is not a final plan status")

        def not_final_yet(entries: list[dict[str, Any]]) -> bool:
            latest = _latest(entries, plan_id)
            return latest is None or latest["kind"] not in FINAL | {"resolved"}

        return self.ledger.append_if(not_final_yet, status, plan_id=plan_id, **details)

    def approve(self, plan_id: str, by: str) -> dict[str, Any]:
        return self.ledger.append("approved", plan_id=plan_id, by=by)

    def note(self, plan_id: str, kind: str, **details: Any) -> dict[str, Any] | None:
        """Record something that is not an outcome, once per state (retries do not repeat it)."""

        def not_just_noted(entries: list[dict[str, Any]]) -> bool:
            latest = _latest(entries, plan_id)
            return latest is None or latest["kind"] != kind

        return self.ledger.append_if(not_just_noted, kind, plan_id=plan_id, **details)

    def claim(self, plan_id: str, authorized_by: str, *, daily_limit: int | None = None) -> bool:
        """Mark a plan as being applied, unless anyone (in any process) already has or has closed it.

        With a daily limit, the claim also fails once `authorized_by` has claimed that many plans in
        the last 24 hours; deciding and recording under the ledger's lock makes the limit exact.
        """

        def open_to_claim(entries: list[dict[str, Any]]) -> bool:
            latest = _latest(entries, plan_id)
            if latest is not None and latest["kind"] in FINAL | {"applying", "resolved"}:
                return False
            if daily_limit is None:
                return True
            since = (dt.datetime.now(dt.UTC) - DAY).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            used = 0
            for entry in reversed(entries):
                if entry["at"] < since:
                    break
                if entry["kind"] == "applying" and entry.get("authorized_by") == authorized_by:
                    used += 1
                elif entry["kind"] == "released" and entry.get("authorized_by") == authorized_by:
                    used -= 1  # nothing was sent, so it does not count
            return used < daily_limit

        entry = self.ledger.append_if(open_to_claim, "applying", plan_id=plan_id, authorized_by=authorized_by)
        return entry is not None

    def used_today(self, authorized_by: str) -> int:
        """Plans `authorized_by` has claimed in the last 24 hours and that were not handed back."""
        since = (dt.datetime.now(dt.UTC) - DAY).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        used = 0
        for entry in reversed(self.ledger.entries()):
            if entry["at"] < since:
                break
            if entry.get("authorized_by") == authorized_by:
                used += {"applying": 1, "released": -1}.get(entry["kind"], 0)
        return used

    def release(self, plan_id: str, reason: str, authorized_by: str | None = None) -> dict[str, Any]:
        """Hand back a claim whose request provably never reached the carrier."""
        return self.ledger.append("released", plan_id=plan_id, reason=reason, authorized_by=authorized_by)

    def resolve(self, plan_id: str, outcome: str, by: str, **details: Any) -> dict[str, Any] | None:
        """An operator records what really happened to a plan in doubt: `applied` or `failed`."""
        if outcome not in ("applied", "failed"):
            raise ValueError("a plan in doubt resolves to applied or failed")

        def in_doubt(entries: list[dict[str, Any]]) -> bool:
            latest = _latest(entries, plan_id)
            return latest is not None and latest["kind"] in ("applying", "unknown")

        return self.ledger.append_if(in_doubt, "resolved", plan_id=plan_id, outcome=outcome, by=by, **details)
