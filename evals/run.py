"""Run the evals.

    uv run python -m evals.run --agent good          # scripted, free: every grader should pass it
    uv run python -m evals.run --agent bad           # scripted, free: every grader should fail it
    uv run python -m evals.run --agent claude --model claude-opus-5 --trials 3 --max-usd 5 --yes
    uv run python -m evals.run --agent claude-code --model claude-opus-5 --trials 3 --max-usd 5 --yes

`claude` calls the Messages API with an API key; `claude-code` runs Claude Code in print mode as the
MCP client, with the login Claude Code already has.

A model run spends money through your Anthropic credentials. It needs --yes, stops starting trials
once --max-usd has been spent, and writes every trial (checks, cost, transcript) to --out, with
run.json recording the commit, the settings and the totals.

A trial that cannot be graded (an API error, a timeout, a grader bug) is recorded as an error: it
is neither a pass nor a fail, what it spent still counts against --max-usd, and the run exits 1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import anyio

from .agents import BAD, GOOD, ClaudeAgent, ClaudeCodeAgent, Scripted, find_claude
from .harness import Agent, Task, Trial, run_trial
from .tasks import BY_ID, TASKS

REPO = Path(__file__).resolve().parent.parent
MAX_USD_PER_TRIAL = 2.0  # Claude Code's own cap on each trial


def wilson(passed: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a pass rate; honest at the small counts evals run at."""
    if total == 0:
        return 0.0, 1.0
    p = passed / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return max(0.0, centre - half), min(1.0, centre + half)


def summary(trials: list[Trial]) -> str:
    by_task: dict[str, list[Trial]] = defaultdict(list)
    for trial in trials:
        by_task[trial.task].append(trial)
    rows = [f"{'task':32} {'passed':>7} {'pass^k':>7} {'errors':>7} {'cost $':>8}  failed checks"]
    for task in TASKS:
        runs = by_task.get(task.id, [])
        if not runs:
            continue
        graded = [t for t in runs if t.error is None]
        passed = sum(t.passed for t in graded)
        failed = sorted({name for t in graded for name, ok in t.checks.items() if not ok})
        every = "yes" if graded and passed == len(graded) else "no"
        rows.append(
            f"{task.id:32} {passed:>3}/{len(graded):<3} {every:>7} {len(runs) - len(graded):>7} "
            f"{sum(t.result.cost_usd for t in runs):>8.3f}  {', '.join(failed)}",
        )
    graded = [t for t in trials if t.error is None]
    total, passed = len(graded), sum(t.passed for t in graded)
    low, high = wilson(passed, total)
    rows.append(
        f"\n{passed}/{total} trials passed ({passed / max(total, 1):.0%}, 95% CI {low:.0%}-{high:.0%}); "
        f"cost ${sum(t.result.cost_usd for t in trials):.3f}",
    )
    if errored := len(trials) - total:
        rows.append(f"{errored} trials errored and are not counted above; see trials.jsonl")
    return "\n".join(rows)


def _commit() -> dict[str, Any]:
    """The code the run measured, so a result can be traced to it without anyone writing it down."""
    unknown: dict[str, Any] = {"commit": None, "uncommitted_changes": None}
    if (git := shutil.which("git")) is None:
        return unknown

    def ask(*arguments: str) -> str:
        done = subprocess.run(  # noqa: S603 - fixed git subcommands, no outside input
            [git, *arguments], cwd=REPO, capture_output=True, text=True, check=True, timeout=10
        )
        return done.stdout.strip()

    try:
        head = ask("rev-parse", "HEAD")
        changes = ask("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        return unknown
    return {"commit": head, "uncommitted_changes": bool(changes)}


def _fresh_directory(parent: Path, name: str) -> Path:
    """A new directory for this run; an earlier run's results are never overwritten."""
    parent.mkdir(parents=True, exist_ok=True)
    for n in range(1000):
        candidate = parent / (name if n == 0 else f"{name}-{n}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise SystemExit(f"could not create a fresh directory for {name} in {parent}")


def agent_for(args: argparse.Namespace) -> Agent:
    if args.agent == "good":
        return Scripted("scripted-good", GOOD)
    if args.agent == "bad":
        return Scripted("scripted-bad", BAD)
    if args.agent == "claude-code":
        binary = args.claude or find_claude()
        if binary is None:
            raise ValueError("no Claude Code found; pass --claude PATH")
        return ClaudeCodeAgent(
            [binary], args.model, max_turns=args.max_turns, budget_usd=min(MAX_USD_PER_TRIAL, args.max_usd)
        )
    return ClaudeAgent(args.model, effort=args.effort, max_turns=args.max_turns)


async def main_async(args: argparse.Namespace, agent: Agent, tasks: tuple[Task, ...]) -> int:
    started = dt.datetime.now(dt.UTC)
    out = _fresh_directory(Path(args.out), f"{started:%Y%m%dT%H%M%SZ}-{agent.name.replace(':', '_')}")
    trials: list[Trial] = []
    spent = 0.0
    skipped = 0
    limiter = anyio.CapacityLimiter(args.jobs)

    with (out / "trials.jsonl").open("w", encoding="utf-8") as log:

        async def one(task: Task, n: int) -> None:
            nonlocal spent, skipped
            async with limiter:
                if spent >= args.max_usd:  # checked as each trial starts; up to --jobs trials may overrun
                    skipped += 1
                    return
                try:
                    trial = await run_trial(task, agent, n)
                except Exception as error:  # setup or harness failed before the agent ran; one must not end the run
                    trial = Trial.errored(task.id, agent.name, n, f"{type(error).__name__}: {error}")
                # Counted before the slot is released, so the next trial to start sees this spend.
                trials.append(trial)
                spent += trial.result.cost_usd
                log.write(json.dumps(trial.record(), default=str) + "\n")
                log.flush()
            mark = "ERROR" if trial.error else ("pass" if trial.passed else "FAIL")
            detail = f": {trial.error}" if trial.error else ""
            print(
                f"{task.id} #{n}: {mark}  (${trial.result.cost_usd:.3f}, {trial.result.turns} turns){detail}",
                file=sys.stderr,
            )

        async with anyio.create_task_group() as group:
            for task in tasks:
                for n in range(1, args.trials + 1):
                    group.start_soon(one, task, n)

    if skipped:
        print(f"skipped {skipped} trials: ${spent:.2f} spent reached the ${args.max_usd:.2f} cap", file=sys.stderr)
    trials.sort(key=lambda t: (t.task, t.trial))
    report = summary(trials)
    (out / "summary.txt").write_text(report + "\n", encoding="utf-8")
    graded = [t for t in trials if t.error is None]
    errored = len(trials) - len(graded)
    meta: dict[str, Any] = {
        "agent": agent.name,
        **_commit(),
        "started": started.isoformat(timespec="seconds"),
        "finished": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "tasks": [t.id for t in tasks],
        "trials_per_task": args.trials,
        "jobs": args.jobs,
        "max_usd": args.max_usd,
        "spent_usd": round(spent, 6),
        "graded": len(graded),
        "passed": sum(t.passed for t in graded),
        "errored": errored,
        "skipped": skipped,
    }
    (out / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(report)
    print(f"\nwritten to {out}", file=sys.stderr)
    return 1 if errored else 0


def _count(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _dollars(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive amount")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--agent", choices=["good", "bad", "claude", "claude-code"], default="good")
    parser.add_argument("--claude", help="the Claude Code binary for --agent claude-code (found automatically)")
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="for --agent claude")
    parser.add_argument("--tasks", default="all", help="comma-separated task ids, or all")
    parser.add_argument("--trials", type=_count, default=1)
    parser.add_argument("--max-turns", type=_count, default=30)
    parser.add_argument("--max-usd", type=_dollars, default=5.0)
    parser.add_argument("--jobs", type=_count, default=4, help="trials run at the same time")
    parser.add_argument("--out", default="evals/out")
    parser.add_argument("--yes", action="store_true", help="confirm a model run spends money")
    args = parser.parse_args(argv)

    if args.agent in ("claude", "claude-code") and not args.yes:
        parser.error("a model run spends money or plan usage through your Anthropic login; pass --yes to confirm")
    if args.effort and args.agent != "claude":
        parser.error("--effort applies only to --agent claude")
    if args.claude and args.agent != "claude-code":
        parser.error("--claude applies only to --agent claude-code")
    if args.tasks == "all":
        tasks = TASKS
    else:
        wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
        if unknown := [t for t in wanted if t not in BY_ID]:
            parser.error(f"unknown tasks {unknown}; known: {', '.join(BY_ID)}")
        if not wanted:
            parser.error("--tasks named no task")
        tasks = tuple(BY_ID[t] for t in dict.fromkeys(wanted))
    try:
        agent = agent_for(args)
    except ValueError as error:
        parser.error(str(error))
    return anyio.run(main_async, args, agent, tasks)


if __name__ == "__main__":
    raise SystemExit(main())
