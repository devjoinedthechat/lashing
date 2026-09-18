"""Run one agent on one task against a fresh simulated carrier, and grade what actually happened.

Grading reads the carrier's state and lashing's ledger, never the agent's own account of what it
did. The agent reaches lashing through a real MCP client, exactly as Claude Desktop would.
"""

from __future__ import annotations

import json
import socket
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import anyio
import uvicorn
from mcp import Client
from mcp_types import ElicitResult

from lashing.config import ACTIONS, DEMO_SHIPPER, Approvals, Config, Grant
from lashing.server import build_server, demo
from lashing.service import Lashing
from lashing.sim import Simulator

Answer = Literal["approve", "say_no"]

SERVER_STARTUP_S = 10.0


class Person:
    """Whoever answers the client's approval prompt: approves or says no. A task with no person has None."""

    def __init__(self, answer: Answer) -> None:
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
    http_url: str | None = None  # lashing over streamable HTTP, for agents that bring their own MCP client

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
    person: Answer | None = None  # None: nobody answers the approval prompt


@dataclass
class AgentResult:
    final_text: str
    turns: int = 0
    cost_usd: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    stopped: str = "end_turn"


class AgentFailed(Exception):
    """An agent that could not finish, such as an API error or a timeout.

    It carries what the agent spent, so a failed trial still counts against the spending cap.
    """

    def __init__(self, message: str, cost_usd: float) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class Agent(Protocol):
    name: str
    approval_prompts: bool  # whether the agent's MCP client lets a (scripted) person answer approval prompts
    needs_http: bool  # whether the agent connects to lashing itself, over HTTP

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
    error: str | None = None  # set when the trial could not be graded; it is then neither a pass nor a fail

    @classmethod
    def errored(
        cls,
        task: str,
        agent: str,
        trial: int,
        error: str,
        cost_usd: float = 0.0,
        *,
        calls: list[Call] | None = None,
        approvals_asked: int = 0,
    ) -> Trial:
        return cls(
            task=task,
            agent=agent,
            trial=trial,
            passed=False,
            checks={},
            result=AgentResult(final_text="", cost_usd=cost_usd, stopped="error"),
            calls=calls or [],
            approvals_asked=approvals_asked,
            error=error,
        )

    def record(self) -> dict[str, Any]:
        """One line of trials.jsonl. Token counts are left out: cost is what the evals report."""
        record = {
            "task": self.task,
            "agent": self.agent,
            "trial": self.trial,
            "passed": self.passed,
            "checks": self.checks,
            "turns": self.result.turns,
            "cost_usd": round(self.result.cost_usd, 6),
            "stopped": self.result.stopped,
            "approvals_asked": self.approvals_asked,
            "final_text": self.result.final_text,
            "calls": [
                {"tool": c.tool, "arguments": c.arguments, "is_error": c.is_error, "result": c.result}
                for c in self.calls
            ],
        }
        if self.error is not None:
            record["error"] = self.error
        return record


RUBBER_STAMP = Grant(id="rubber-stamp", actions=ACTIONS)
"""What a person who approves everything amounts to, for clients that cannot show approval prompts."""


@asynccontextmanager
async def _served_over_http(service: Lashing) -> AsyncIterator[str]:
    """lashing's MCP server on a free local port for the length of a trial; yields its URL."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = build_server(service).streamable_http_app()
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        async with anyio.create_task_group() as group:
            group.start_soon(server.serve, [listener])
            with anyio.fail_after(SERVER_STARTUP_S):
                while not server.started:
                    if server.should_exit:  # uvicorn gave up during startup
                        raise RuntimeError("lashing's HTTP server did not start")
                    await anyio.sleep(0.01)
            try:
                yield f"http://127.0.0.1:{port}/mcp"
            finally:
                server.should_exit = True


@asynccontextmanager
async def environment(
    task: Task,
    state_dir: Path,
    *,
    approval_prompts: bool = True,
    http: bool = False,
) -> AsyncIterator[tuple[World, Session, Person | None]]:
    sim = Simulator()
    facts = task.setup(sim)
    grants, person_answer = task.grants, task.person
    if not approval_prompts and person_answer is not None:
        # The client cannot ask anyone. A person who approves everything is a grant for everything;
        # a person who says no is no grant at all. The graders see the same outcomes either way.
        grants = (*grants, RUBBER_STAMP) if person_answer == "approve" else grants
        person_answer = None
    config = Config(
        endpoints=None,
        shipper=DEMO_SHIPPER,
        grants=grants,
        approvals=Approvals(client=approval_prompts, operator=True),
        state_dir=state_dir,
    )
    service, _ = demo(state_dir, sim=sim, config=config)
    person = Person(person_answer) if person_answer else None
    async with Client(build_server(service), elicitation_callback=person) as client:
        listed = (await client.list_tools()).tools
        tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in listed]
        session = Session(client, tools, client.instructions or "")
        if http:
            async with _served_over_http(service) as url:
                session.http_url = url
                yield World(sim, service, facts), session, person
        else:
            yield World(sim, service, facts), session, person


async def run_trial(task: Task, agent: Agent, trial: int) -> Trial:
    """Run and grade one trial. An agent that fails, or a grader that cannot grade, gives an errored trial."""
    with tempfile.TemporaryDirectory(prefix="lashing-eval-") as tmp:
        async with environment(
            task,
            Path(tmp),
            approval_prompts=getattr(agent, "approval_prompts", True),
            http=getattr(agent, "needs_http", False),
        ) as (world, session, person):

            def asked() -> int:
                return len(person.asked) if person else 0

            prompt = task.prompt(world.facts)
            try:
                result = await agent.run(session, prompt, task=task, today=world.sim.now.date().isoformat())
            except AgentFailed as failure:
                return Trial.errored(
                    task.id,
                    agent.name,
                    trial,
                    str(failure),
                    failure.cost_usd,
                    calls=session.calls,
                    approvals_asked=asked(),
                )
            try:
                checks = task.grade(world, session, result.final_text)
            except Exception as error:  # a grader bug must not lose what the trial spent
                return Trial.errored(
                    task.id,
                    agent.name,
                    trial,
                    f"grading failed: {type(error).__name__}: {error}",
                    result.cost_usd,
                    calls=session.calls,
                    approvals_asked=asked(),
                )
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
