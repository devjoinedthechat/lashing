"""The eval graders, proven on scripted agents before any model time is spent on them, and the runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals import run
from evals.agents import BAD, GOOD, REFERENCE, ClaudeAgent, ClaudeCodeAgent, Scripted, cost
from evals.harness import RUBBER_STAMP, AgentFailed, AgentResult, Session, Task, environment, run_trial
from evals.tasks import BY_ID, TASKS

pytestmark = pytest.mark.anyio
FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def test_every_task_has_a_good_and_a_bad_policy() -> None:
    ids = {t.id for t in TASKS}
    assert set(GOOD) == ids == set(BAD)


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
async def test_a_correct_agent_passes(task: Task) -> None:
    trial = await run_trial(task, Scripted("good", GOOD), 1)
    assert trial.passed, trial.checks


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
async def test_the_mistake_each_task_targets_is_caught(task: Task) -> None:
    trial = await run_trial(task, Scripted("bad", BAD), 1)
    assert not trial.passed, trial.checks


# -- the model loop -------------------------------------------------------------------------------------


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


class _OverloadedAfterOneTurn(_ScriptedModel):
    async def create(self, **request: Any) -> Any:
        if self.requests:
            raise ConnectionError("overloaded")
        return await super().create(**request)


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


async def test_an_api_failure_is_an_error_that_keeps_what_the_turns_before_it_cost() -> None:
    agent = ClaudeAgent("claude-opus-5")
    agent.client = _OverloadedAfterOneTurn()  # type: ignore[assignment]
    trial = await run_trial(BY_ID["ignore-injected-instruction"], agent, 1)
    assert trial.error is not None and "overloaded" in trial.error
    assert not trial.passed and trial.checks == {}
    assert [c.tool for c in trial.calls] == ["get_booking"]  # what it did before failing is kept
    assert trial.result.cost_usd == pytest.approx(cost("claude-opus-5", {"input_tokens": 1000, "output_tokens": 100}))


def test_a_model_without_a_price_is_refused_so_the_spend_cap_holds() -> None:
    with pytest.raises(ValueError, match="no price"):
        ClaudeAgent("claude-some-future-model")


# -- Claude Code as the client ------------------------------------------------------------------------------


async def test_claude_code_runs_as_the_mcp_client_over_http() -> None:
    """The whole path without spending anything: HTTP server, MCP config, clean environment, stream-json."""
    agent = ClaudeCodeAgent([sys.executable, str(FAKE_CLAUDE)])
    trial = await run_trial(BY_ID["ignore-injected-instruction"], agent, 1)
    assert trial.passed, trial.checks
    assert [(c.tool, c.is_error) for c in trial.calls] == [("get_booking", False)]
    assert trial.calls[0].result["reference"].startswith("LSIM")
    assert trial.result.cost_usd == 0.0123
    assert "leaked=[]" in trial.result.final_text  # no CLAUDE*, VSCODE* or MCP_* variables reach the child


async def test_a_claude_code_run_that_hangs_is_stopped_and_charged_its_budget() -> None:
    hangs = ClaudeCodeAgent([sys.executable, "-c", "import time; time.sleep(60)"], budget_usd=0.5, timeout_s=0.5)
    trial = await run_trial(BY_ID["refuse-impossible-change"], hangs, 1)
    assert trial.error is not None and "did not finish" in trial.error
    assert trial.result.cost_usd == 0.5


def _stream(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_claude_code_output_that_cannot_be_graded_is_an_error_with_a_cost() -> None:
    agent = ClaudeCodeAgent(["claude"], budget_usd=0.75)
    session = Session(client=None, tools=[], instructions="")  # type: ignore[arg-type]
    with pytest.raises(AgentFailed, match="without a result") as no_result:
        agent._read("", "crashed", session)
    assert no_result.value.cost_usd == 0.75  # unknown, so the most it was allowed

    unreachable = _stream(
        {"type": "system", "subtype": "init", "mcp_servers": [{"name": "lashing", "status": "failed"}]},
        {"type": "result", "subtype": "success", "result": "I could not reach it.", "total_cost_usd": 0.02},
    )
    with pytest.raises(AgentFailed, match="could not connect") as not_connected:
        agent._read(unreachable, "", session)
    assert not_connected.value.cost_usd == 0.02


def test_a_tool_call_the_session_never_answered_is_still_recorded() -> None:
    call = {"type": "tool_use", "id": "t1", "name": "mcp__lashing__propose_cancellation", "input": {"reference": "X"}}
    cut_off = _stream(
        {"type": "system", "subtype": "init", "mcp_servers": [{"name": "lashing", "status": "connected"}]},
        {"type": "assistant", "message": {"content": [call]}},
        {"type": "result", "subtype": "error_max_turns", "result": "", "num_turns": 30, "total_cost_usd": 0.1},
    )
    session = Session(client=None, tools=[], instructions="")  # type: ignore[arg-type]
    result = ClaudeCodeAgent(["claude"])._read(cut_off, "", session)
    assert result.stopped == "error_max_turns"
    assert [(c.tool, c.result, c.is_error) for c in session.calls] == [("propose_cancellation", None, True)]


async def test_without_approval_prompts_a_rubber_stamp_person_becomes_a_grant(tmp_path: Path) -> None:
    task = BY_ID["rebook-late-shipment"]  # its person approves everything
    async with environment(task, tmp_path, approval_prompts=False) as (world, _, person):
        assert person is None
        assert RUBBER_STAMP in world.service.config.grants
        assert not world.service.config.approvals.client


# -- the runner -----------------------------------------------------------------------------------------


def _run_dir(out: Path) -> Path:
    (found,) = out.iterdir()
    return found


def test_a_run_writes_its_trials_a_summary_and_where_they_came_from(tmp_path: Path) -> None:
    code = run.main(
        ["--agent", "good", "--tasks", "leave-on-time-shipment,cancel-the-right-one", "--out", str(tmp_path)]
    )
    assert code == 0
    written = _run_dir(tmp_path)
    trials = [json.loads(line) for line in (written / "trials.jsonl").read_text().splitlines()]
    assert sorted(t["task"] for t in trials) == ["cancel-the-right-one", "leave-on-time-shipment"]
    assert all(t["passed"] and "usage" not in t and "error" not in t for t in trials)
    meta = json.loads((written / "run.json").read_text())
    assert (meta["graded"], meta["passed"], meta["errored"], meta["skipped"]) == (2, 2, 0, 0)
    assert "commit" in meta and "uncommitted_changes" in meta and meta["started"] <= meta["finished"]
    assert "2/2 trials passed" in (written / "summary.txt").read_text()


class _Failing:
    """An agent whose every trial fails after spending $0.50."""

    name = "failing"
    approval_prompts = True
    needs_http = False

    async def run(self, session: Session, prompt: str, *, task: Task, today: str) -> AgentResult:
        raise AgentFailed("the API is down", 0.5)


def test_errored_trials_count_against_the_cap_are_not_graded_and_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "agent_for", lambda args: _Failing())
    argv = ["--tasks", "leave-on-time-shipment", "--trials", "3", "--jobs", "1", "--max-usd", "0.4"]
    code = run.main([*argv, "--out", str(tmp_path)])
    assert code == 1
    written = _run_dir(tmp_path)
    meta = json.loads((written / "run.json").read_text())
    # The first trial spent past the cap, so the other two never started.
    assert (meta["graded"], meta["errored"], meta["skipped"], meta["spent_usd"]) == (0, 1, 2, 0.5)
    (trial,) = [json.loads(line) for line in (written / "trials.jsonl").read_text().splitlines()]
    assert trial["error"] == "the API is down" and trial["passed"] is False
    assert "1 trials errored" in (written / "summary.txt").read_text()


def test_a_second_run_in_the_same_second_does_not_overwrite_the_first(tmp_path: Path) -> None:
    first = run._fresh_directory(tmp_path, "20260918T000000Z-good")
    second = run._fresh_directory(tmp_path, "20260918T000000Z-good")
    assert first != second and first.is_dir() and second.is_dir()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--tasks", "book-to-deadline,no-such-task"], "unknown tasks"),
        (["--jobs", "0"], "at least 1"),
        (["--max-usd", "-1"], "positive"),
        (["--effort", "high"], "only to --agent claude"),
        (["--claude", "/bin/claude"], "only to --agent claude-code"),
        (["--agent", "claude"], "pass --yes"),
        (["--agent", "claude", "--model", "claude-some-future-model", "--yes"], "no price"),
    ],
)
def test_bad_arguments_are_refused_before_anything_runs(
    argv: list[str], message: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_:
        run.main([*argv, "--out", str(tmp_path)])
    assert exit_.value.code == 2
    assert message in capsys.readouterr().err
    assert not any(tmp_path.iterdir())


def test_the_interval_is_honest_at_small_counts() -> None:
    low, high = run.wilson(6, 10)
    assert (round(low, 2), round(high, 2)) == (0.31, 0.83)
    assert run.wilson(0, 0) == (0.0, 1.0)
