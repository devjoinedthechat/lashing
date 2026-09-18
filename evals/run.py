"""Run the evals.

    uv run python -m evals.run --agent good          # scripted, free: every grader should pass it
    uv run python -m evals.run --agent bad           # scripted, free: every grader should fail it
    uv run --group evals python -m evals.run --agent claude --model claude-opus-5 --trials 3 --max-usd 5 --yes

A model run spends money through your Anthropic credentials. It needs --yes, stops starting trials
once --max-usd has been spent, and writes every trial (checks, cost, transcript) to --out.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import anyio

from .agents import BAD, GOOD, ClaudeAgent, Scripted
from .harness import Agent, Task, Trial, run_trial
from .tasks import BY_ID, TASKS


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
    rows = [f"{'task':32} {'passed':>7} {'pass^k':>7} {'cost $':>8}  failed checks"]
    for task in TASKS:
        runs = by_task.get(task.id, [])
        if not runs:
            continue
        passed = sum(t.passed for t in runs)
        failed = sorted({name for t in runs for name, ok in t.checks.items() if not ok})
        rows.append(
            f"{task.id:32} {passed:>3}/{len(runs):<3} {'yes' if passed == len(runs) else 'no':>7} "
            f"{sum(t.result.cost_usd for t in runs):>8.3f}  {', '.join(failed)}",
        )
    total, passed = len(trials), sum(t.passed for t in trials)
    low, high = wilson(passed, total)
    rows.append(
        f"\n{passed}/{total} trials passed ({passed / max(total, 1):.0%}, 95% CI {low:.0%}-{high:.0%}); "
        f"cost ${sum(t.result.cost_usd for t in trials):.3f}",
    )
    return "\n".join(rows)


def agent_for(args: argparse.Namespace) -> Agent:
    if args.agent == "good":
        return Scripted("scripted-good", GOOD)
    if args.agent == "bad":
        return Scripted("scripted-bad", BAD)
    return ClaudeAgent(args.model, effort=args.effort, max_turns=args.max_turns)


async def main_async(args: argparse.Namespace) -> int:
    tasks = TASKS if args.tasks == "all" else tuple(BY_ID[t] for t in args.tasks.split(","))
    agent = agent_for(args)
    out = Path(args.out) / f"{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}-{agent.name.replace(':', '_')}"
    out.mkdir(parents=True, exist_ok=True)
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
                except Exception as error:  # one broken trial must not end the run
                    print(f"{task.id} #{n}: error {type(error).__name__}: {error}", file=sys.stderr)
                    return
            trials.append(trial)
            spent += trial.result.cost_usd
            log.write(json.dumps(trial.record(), default=str) + "\n")
            log.flush()
            mark = "pass" if trial.passed else "FAIL"
            print(
                f"{task.id} #{n}: {mark}  (${trial.result.cost_usd:.3f}, {trial.result.turns} turns)", file=sys.stderr
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
    meta: dict[str, Any] = {
        "agent": agent.name,
        "trials_per_task": args.trials,
        "tasks": [t.id for t in tasks],
        "skipped": skipped,
    }
    (out / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(report)
    print(f"\nwritten to {out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--agent", choices=["good", "bad", "claude"], default="good")
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--tasks", default="all", help="comma-separated task ids, or all")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-usd", type=float, default=5.0)
    parser.add_argument("--jobs", type=int, default=4, help="trials run at the same time")
    parser.add_argument("--out", default="evals/out")
    parser.add_argument("--yes", action="store_true", help="confirm a model run spends money")
    args = parser.parse_args(argv)
    if args.agent == "claude" and not args.yes:
        parser.error("a model run spends money through your Anthropic credentials; pass --yes to confirm")
    return anyio.run(main_async, args)


if __name__ == "__main__":
    raise SystemExit(main())
