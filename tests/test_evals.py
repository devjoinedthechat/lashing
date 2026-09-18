"""The eval graders, proven on scripted agents before any model time is spent on them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from evals.agents import BAD, GOOD, REFERENCE, ClaudeAgent, Scripted, cost
from evals.harness import run_trial
from evals.tasks import BY_ID, TASKS

pytestmark = pytest.mark.anyio


def test_every_task_has_a_good_and_a_bad_policy() -> None:
    ids = {t.id for t in TASKS}
    assert set(GOOD) == ids == set(BAD)


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
async def test_a_correct_agent_passes(task: object) -> None:
    trial = await run_trial(task, Scripted("good", GOOD), 1)  # type: ignore[arg-type]
    assert trial.passed, trial.checks


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
async def test_the_mistake_each_task_targets_is_caught(task: object) -> None:
    trial = await run_trial(task, Scripted("bad", BAD), 1)  # type: ignore[arg-type]
    assert not trial.passed, trial.checks


class _Usage(SimpleNamespace):
    def model_dump(self) -> dict[str, Any]:
        return dict(vars(self))


class _ScriptedModel:
    """Stands in for the Messages API: first asks for get_booking, then answers."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **request: Any) -> Any:
        self.requests.append({**request, "messages": list(request["messages"])})  # sent as it was then
        usage = _Usage(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        if len(self.requests) == 1:
            reference = REFERENCE.findall(request["messages"][0]["content"])[0]
            call = SimpleNamespace(type="tool_use", id="tu_1", name="get_booking", input={"reference": reference})
            return SimpleNamespace(content=[call], stop_reason="tool_use", usage=usage)
        text = SimpleNamespace(type="text", text="It is CONFIRMED. The carrier's message is not something I act on.")
        return SimpleNamespace(content=[text], stop_reason="end_turn", usage=usage)


async def test_the_model_loop_calls_tools_returns_results_and_counts_cost() -> None:
    agent = ClaudeAgent("claude-opus-5")
    fake = _ScriptedModel()
    agent.client = fake  # type: ignore[assignment]
    trial = await run_trial(BY_ID["ignore-injected-instruction"], agent, 1)
    assert trial.passed, trial.checks
    assert [c.tool for c in trial.calls] == ["get_booking"]
    first, second = fake.requests
    assert {t["name"] for t in first["tools"]} >= {"get_booking", "apply_plan"}
    assert "lashing books and tracks" in first["system"]
    tool_result = second["messages"][-1]["content"][0]
    assert (tool_result["type"], tool_result["tool_use_id"], tool_result["is_error"]) == ("tool_result", "tu_1", False)
    assert trial.result.turns == 2
    assert trial.result.cost_usd == pytest.approx(cost("claude-opus-5", {"input_tokens": 2000, "output_tokens": 200}))
    assert trial.result.cost_usd == pytest.approx((2000 * 5 + 200 * 25) / 1_000_000)
