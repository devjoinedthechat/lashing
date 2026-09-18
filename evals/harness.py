"""Run one agent on one task against a fresh simulated carrier, and grade what actually happened.

Grading reads the carrier's state and lashing's ledger, never the agent's own account of what it
did. The agent reaches lashing through a real MCP client, exactly as Claude Desktop would.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mcp import Client
from mcp_types import ElicitResult

from lashing.config import DEMO_SHIPPER, Approvals, Config, Grant
from lashing.server import build_server, demo
from lashing.service import Lashing
from lashing.sim import Simulator


class Person:
    """Whoever answers the client's approval prompt: approves, says no, or is not there (None)."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.asked.append(params.message)
        if self.answer == "approve":
            return ElicitResult(action="accept", content={"approve": True})
        return ElicitResult(action="accept", content={"approve": False})


@dataclass
class Call:
    tool: str
    arguments: dict[str, Any]
    result: Any
    is_error: bool


@dataclass
class Session:
    """The agent's handle on lashing: the tool list and a way to call tools, all recorded."""

    client: Client
    tools: list[dict[str, Any]]
    instructions: str
    calls: list[Call] = field(default_factory=list)

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        result = await self.client.call_tool(name, arguments)
        text = "".join(getattr(block, "text", "") for block in result.content)
        payload: Any = result.structured_content if result.structured_content is not None else text
        self.calls.append(Call(name, arguments, payload, bool(result.is_error)))
        return text, bool(result.is_error)

    async def use(self, name: str, **arguments: Any) -> dict[str, Any]:
        """For scripted agents: call a tool and get its structured result (raises on tool errors)."""
        text, failed = await self.call(name, arguments)
        if failed:
            raise ToolFailedError(text)
        result = self.calls[-1].result
        return result if isinstance(result, dict) else json.loads(text)

    def called(self, tool: str) -> list[Call]:
        return [c for c in self.calls if c.tool == tool]


class ToolFailedError(Exception):
    pass


@dataclass
class World:
    """What a task's setup and grader work with."""

    sim: Simulator
    service: Lashing
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    id: str
    summary: str
    setup: Callable[[Simulator], dict[str, Any]]
    prompt: Callable[[dict[str, Any]], str]
    grade: Callable[[World, Session, str], dict[str, bool]]
    grants: tuple[Grant, ...] = ()
    person: str | None = None  # "approve", "say_no", or None for a client without approval prompts


@dataclass
class AgentResult:
    final_text: str
    turns: int = 0
    cost_usd: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    stopped: str = "end_turn"


class Agent(Protocol):
    name: str

    async def run(self, session: Session, prompt: str, *, task: Task, today: str) -> AgentResult: ...


@dataclass
class Trial:
    task: str
    agent: str
    trial: int
    passed: bool
    checks: dict[str, bool]
    result: AgentResult
    calls: list[Call]
    approvals_asked: int

    def record(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "agent": self.agent,
            "trial": self.trial,
            "passed": self.passed,
            "checks": self.checks,
            "turns": self.result.turns,
            "cost_usd": round(self.result.cost_usd, 6),
            "usage": self.result.usage,
            "stopped": self.result.stopped,
            "approvals_asked": self.approvals_asked,
            "final_text": self.result.final_text,
            "calls": [
                {"tool": c.tool, "arguments": c.arguments, "is_error": c.is_error, "result": c.result}
                for c in self.calls
            ],
        }


@asynccontextmanager
async def environment(task: Task, state_dir: Path) -> AsyncIterator[tuple[World, Session, Person | None]]:
    sim = Simulator()
    facts = task.setup(sim)
    config = Config(
        endpoints=None,
        shipper=DEMO_SHIPPER,
        grants=task.grants,
        approvals=Approvals(client=True, operator=True),
        state_dir=state_dir,
    )
    service, _ = demo(state_dir, sim=sim, config=config)
    person = Person(task.person) if task.person else None
    async with Client(build_server(service), elicitation_callback=person) as client:
        listed = (await client.list_tools()).tools
        tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in listed]
        session = Session(client, tools, client.instructions or "")
        yield World(sim, service, facts), session, person


async def run_trial(task: Task, agent: Agent, trial: int) -> Trial:
    with tempfile.TemporaryDirectory(prefix="lashing-eval-") as tmp:
        async with environment(task, Path(tmp)) as (world, session, person):
            prompt = task.prompt(world.facts)
            result = await agent.run(session, prompt, task=task, today=world.sim.now.date().isoformat())
            checks = task.grade(world, session, result.final_text)
    return Trial(
        task=task.id,
        agent=agent.name,
        trial=trial,
        passed=all(checks.values()),
        checks=checks,
        result=result,
        calls=session.calls,
        approvals_asked=len(person.asked) if person else 0,
    )


Scripted = Callable[[Session, dict[str, Any]], Awaitable[str]]
