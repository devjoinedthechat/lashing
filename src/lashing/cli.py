"""The `lashing` command."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from lashing import __version__
from lashing.carrier import HttpCarrier
from lashing.config import Config, ConfigError, load
from lashing.ledger import Ledger
from lashing.plans import PlanBook


def default_state_dir() -> Path:
    if env := os.environ.get("LASHING_STATE_DIR"):
        return Path(env)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "lashing"


def _config(args: argparse.Namespace) -> Config:
    config = load(Path(args.config)) if getattr(args, "config", None) else None
    if config is not None:
        return config
    from lashing.config import DEMO_SHIPPER  # noqa: PLC0415

    return Config(endpoints=None, shipper=DEMO_SHIPPER, state_dir=Path(args.state_dir or default_state_dir() / "demo"))


def _start(value: str | None) -> dt.datetime:
    if value:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)
    return dt.datetime.now(dt.UTC).replace(hour=8, minute=0, second=0, microsecond=0)


def cmd_demo(args: argparse.Namespace) -> int:
    from lashing.server import build_server, demo  # noqa: PLC0415
    from lashing.sim import Simulator  # noqa: PLC0415

    config = _config(args)
    service, _ = demo(config.state_dir, sim=Simulator(_start(args.start)), config=config)
    build_server(service).run("stdio")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from lashing.server import build_server  # noqa: PLC0415
    from lashing.service import Lashing  # noqa: PLC0415

    config = load(Path(args.config))
    if config.endpoints is None:
        print("lashing serve: the config has no [carrier]; use `lashing demo` for the simulator", file=sys.stderr)
        return 2
    carrier = HttpCarrier(config.endpoints, headers=config.headers, validation=config.validation)
    build_server(Lashing(config, carrier)).run("stdio")
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    import uvicorn  # noqa: PLC0415

    from lashing.sim import Simulator  # noqa: PLC0415
    from lashing.sim.app import create_app  # noqa: PLC0415

    sim = Simulator(_start(args.start))
    print(f"simulated carrier at http://{args.host}:{args.port} (clock {sim.now.isoformat()})", file=sys.stderr)
    uvicorn.run(create_app(sim), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_plans(args: argparse.Namespace) -> int:
    book = PlanBook(Ledger(_config(args).state_dir / "ledger.jsonl"))
    open_plans = [s.plan.view() | ({"approved_by": s.approved_by} if s.approved_by else {}) for s in book.open()]
    print(json.dumps(open_plans, indent=2))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    book = PlanBook(Ledger(_config(args).state_dir / "ledger.jsonl"))
    state = book.get(args.plan_id)
    if state is None or state.status != "proposed":
        print(f"no open plan {args.plan_id}", file=sys.stderr)
        return 1
    print(json.dumps(state.plan.view() | {"payload": state.plan.payload}, indent=2))
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing to approve without a terminal; pass --yes to approve non-interactively", file=sys.stderr)
            return 1
        typed = input(f"\nType the plan id to approve it ({args.plan_id}): ").strip()
        if typed != args.plan_id:
            print("not approved", file=sys.stderr)
            return 1
    name = args.as_ or getpass.getuser()
    book.approve(args.plan_id, by=f"operator:{name}")
    print(f"approved as operator:{name}; the agent can now call apply_plan({args.plan_id!r})")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    ledger = Ledger(_config(args).state_dir / "ledger.jsonl")
    if args.action == "head":
        print(ledger.head())
        return 0
    if args.action == "show":
        for entry in ledger.entries():
            print(json.dumps(entry))
        return 0
    result = ledger.verify()
    if result.ok:
        print(f"ok: {result.entries} entries, head {result.head}")
        return 0
    print(f"TAMPERED: {result.problem}", file=sys.stderr)
    return 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="lashing", description=__doc__)
    root.add_argument("--version", action="version", version=f"lashing {__version__}")
    commands = root.add_subparsers(dest="command", required=True)

    def with_state(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--config", help="a lashing.toml; its state_dir is used")
        p.add_argument("--state-dir", help=f"where the ledger lives (default {default_state_dir()}/demo)")
        return p

    demo = with_state(commands.add_parser("demo", help="MCP server over stdio with a built-in simulated carrier"))
    demo.add_argument("--start", help="the simulator's clock, ISO 8601 (default: today 08:00 UTC)")
    demo.set_defaults(run=cmd_demo)

    serve = commands.add_parser("serve", help="MCP server over stdio against the carrier in --config")
    serve.add_argument("--config", required=True)
    serve.set_defaults(run=cmd_serve)

    sim = commands.add_parser("sim", help="run the simulated carrier over HTTP")
    sim.add_argument("--host", default="127.0.0.1")
    sim.add_argument("--port", type=int, default=8401)
    sim.add_argument("--start", help="the simulator's clock, ISO 8601 (default: today 08:00 UTC)")
    sim.set_defaults(run=cmd_sim)

    with_state(commands.add_parser("plans", help="list plans waiting to be applied")).set_defaults(run=cmd_plans)

    approve = with_state(commands.add_parser("approve", help="approve a plan as an operator"))
    approve.add_argument("plan_id")
    approve.add_argument("--as", dest="as_", help="the approver's name (default: your OS user)")
    approve.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    approve.set_defaults(run=cmd_approve)

    ledger = with_state(commands.add_parser("ledger", help="verify or read the ledger"))
    ledger.add_argument("action", choices=["verify", "head", "show"])
    ledger.set_defaults(run=cmd_ledger)
    return root


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    args = parser().parse_args(argv)
    try:
        code: int = args.run(args)
    except ConfigError as error:
        print(f"lashing: {error}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
