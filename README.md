# lashing

An MCP server that lets AI agents book and track container shipments through the
[DCSA](https://dcsa.org) open standards, with every change validated, authorized and logged.

Lashing is what stops cargo from shifting at sea. This project does the same for agents that act
on bookings. An agent can propose any change, but nothing reaches a carrier until it conforms to
the standard, is covered by a grant or approved by a person, and is recorded in a tamper-evident
ledger.

> **Status: pre-alpha.** It works end to end against the built-in simulated carrier. It has not
> been run against a production carrier. The status table below lists what is verified.

## Try it

You need [uv](https://docs.astral.sh/uv/). The demo runs against a simulated carrier inside the
same process: no account, no network, no credentials.

```sh
git clone https://github.com/devjoinedthechat/lashing && cd lashing
uv sync
claude mcp add lashing -- uv --directory "$PWD" run lashing demo    # Claude Code
```

For Claude Desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lashing": { "command": "uv", "args": ["--directory", "/path/to/lashing", "run", "lashing", "demo"] }
  }
}
```

Then ask something like *"Find a sailing from Shanghai to Rotterdam next week and book two
40-foot high cubes of furniture, 18 tonnes each."* The demo has no grants, so every write
stops for your approval.

## Tools

| Tool | What it does |
|---|---|
| `find_sailings` | Point-to-point schedules, earliest arrival first, with cut-offs and a `routing_reference` |
| `get_booking` | Status, what it means, `allowed_actions` in this state, route, cut-offs, equipment |
| `track_shipment` | Vessel calls with planned, estimated and actual times; delays; container moves |
| `list_bookings`, `list_plans` | What this instance has written, and what is waiting |
| `propose_booking` | A new booking request, as a plan |
| `propose_change` | An update before confirmation or an amendment after; lashing picks which |
| `propose_cancellation` | The right one of DCSA's three cancellation forms for the booking's state |
| `apply_plan` | Sends a plan, once, if a grant covers it or a person approves it |
| `discard_plan` | Drops a plan so it can never be sent |

Read tools are annotated read-only. `apply_plan` is the only tool that changes anything at the
carrier.

## How a write happens

1. **Propose.** The agent calls a `propose_*` tool. lashing builds the exact DCSA request body
   and validates it against the vendored schema. For a change, it reads the booking, works out
   whether the standard calls it an update or an amendment, diffs it field by field, and
   fingerprints the booking. The agent gets back a plan id, a summary and the diff. Nothing has
   been sent.
2. **Authorize.** `apply_plan` looks for authority, in this order:
   - a **grant** in the operator's config covering the action, booking, lane, fields and units;
   - an **operator approval**, from `lashing approve <plan>` run in a terminal;
   - a **person's yes** to the MCP client's approval prompt. lashing uses MCP elicitation, and on
     protocol 2026-07-28 the question travels as an input-required result.

   The approval is never a tool argument, so the model can't supply it.
3. **Apply.** lashing re-reads the booking and refuses the plan if it changed since it was
   proposed. It claims the plan under a file lock, so it is sent at most once even with several
   server processes, then sends it.
4. **Record.** Every proposal, approval, refusal and write is appended to a hash-chained JSONL
   ledger. `lashing ledger verify` detects edits, deletions and reordering.

Party details (booking agent, shipper, contacts, service contract) come from the config, never
from the model. Carrier-written text (feedback, delay reasons) is cleaned, capped at 500
characters, and returned only under `carrier_says`, with a notice that it is data, not
instructions.

## Configuration

`lashing serve --config lashing.toml` runs against a real carrier.
[lashing.example.toml](lashing.example.toml) is annotated. The example grants:

```toml
[[grant]]
id = "rebook-to-another-sailing"
actions = ["amend"]
fields = ["routingReference", "expectedDepartureDate"]

[[grant]]
id = "small-asia-europe-bookings"
actions = ["create"]
lanes = ["CN*-NL*", "CN*-DE*", "CN*-BE*"]
max_units = 4
expires = 2026-12-31
```

Credentials are read from an environment variable named in the config. They are never written to
the config, the ledger or any tool result.

## Threat model

lashing assumes the model can be wrong or manipulated, and that the carrier's text can be
hostile. The tests in [tests/test_safety.py](tests/test_safety.py) attack each of these directly.

**Defended:**
- **Carrier text asks the agent to act.** A test makes the agent follow an injected instruction
  word for word: it proposes the cancellation and calls `apply_plan`. Nothing is sent, because
  authority comes only from grants and people.
- **Retries, duplicates and races.** A plan is sent at most once, including two server processes
  racing on one ledger.
- **Acting on stale information.** A plan whose booking changed at the carrier is refused.
- **Malformed or invented requests.** A made-up plan id sends nothing. A non-conformant body is
  refused by the client before any request, even one forged into the ledger. Booking references
  are percent-encoded, so they can't reshape the URL.
- **Editing the record afterwards.** Edits, deletions and reordering break the hash chain.

**Not defended, by design:**
- **An agent with a shell running as the same OS user.** It can run `lashing approve --yes` or
  edit the config or the ledger. Run lashing's state and config under a different user or in a
  container when the agent has a shell.
- **A client that answers approval prompts by itself.** Set `approvals.client = false` for such
  clients.
- **Rewriting the whole ledger.** Someone with write access can rebuild the entire chain.
  `lashing ledger head` prints the latest hash so you can anchor it elsewhere.
- **Wrong reads.** Carrier text can still mislead the agent's answers. Only writes are guarded.

## The simulated carrier

`lashing.sim` is a carrier you can run with no access to anyone's systems. Its network uses real
UN/LOCODEs and fictional services, vessels, IMO numbers and container numbers, all with valid
check digits. Voyages run weekly from Asia to North Europe, the US West Coast and the Gulf, and a
North Europe feeder provides transshipments.

`lashing sim --port 8401` serves the provider side of the three standards over HTTP:

| Path | Standard |
|---|---|
| `POST /bkg/v2/bookings`, `GET/PUT/PATCH /bkg/v2/bookings/{reference}` | Booking 2.0.5 |
| `GET /cs/v1/point-to-point-routes` | Commercial Schedules 1.0.4 |
| `GET /tnt/v3/events` | Track & Trace 3.0.0 |

The booking desk behaves like a carrier's:
- It confirms a booking when a sailing has space and its cut-off has not passed.
- It asks for an update when the cargo weight is missing or the vessel is full.
- It confirms or declines amendments.
- It declines a cancellation once the cargo has sailed.

Tracking events accumulate as a real feed's do: planned, then estimated when a voyage is
delayed, then actual. Scenario controls (`/_sim/advance`, `/_sim/delay`, `/_sim/override`) move
the clock, delay a voyage, or make the carrier ask for changes, reject, decline or say anything in
its feedback. The tests check every response, errors included, against the DCSA schemas.

## Standards

| Standard | Version | Source |
|---|---|---|
| Booking | 2.0.5 | [dcsaorg/DCSA-OpenAPI](https://github.com/dcsaorg/DCSA-OpenAPI) |
| Commercial Schedules | 1.0.4 | [dcsaorg/DCSA-OpenAPI](https://github.com/dcsaorg/DCSA-OpenAPI) |
| Track & Trace | 3.0.0 | [dcsaorg/Conformance-Gateway](https://github.com/dcsaorg/Conformance-Gateway) |

The exact commits are in [src/lashing/dcsa/specs/SOURCES.json](src/lashing/dcsa/specs/SOURCES.json).
Regenerate them with `uv run python scripts/vendor_specs.py`. Track & Trace comes from the
Conformance Gateway because DCSA-OpenAPI's main branch still carries the 3.0.0 beta.

## Status

Last updated 2026-09-18.

| Area | State |
|---|---|
| DCSA specs vendored at pinned commits; payloads validated, tested on DCSA's own examples and Conformance Framework samples | ✅ |
| Booking lifecycle rules: update vs amendment, the three cancellation forms, which reference each call uses | ✅ |
| Simulated carrier over HTTP: bookings, schedules, tracking, delays, scenario controls | ✅ |
| DCSA HTTP client with request validation, response validation (strict, warn or off) and error mapping | ✅ |
| Plans, grants, operator and client approvals, at-most-once apply, stale-plan refusal, hash-chained ledger | ✅ |
| MCP server (10 tools) and CLI: `demo`, `serve`, `sim`, `plans`, `approve`, `ledger` | ✅ |
| Attack suite for the six safety invariants | ✅ |
| Run against the DCSA Conformance Framework | ⬜ |
| Agent evals across models | ⬜ |
| Track & Trace 2.2 (widely deployed), eBL 3.0 | ⬜ |

## Development

```sh
uv sync
uv run pytest            # about 160 tests, under 5 seconds
uv run ruff check . && uv run mypy
```

## License

Apache-2.0. See [NOTICE](NOTICE) for the DCSA material this project includes.

lashing is an independent project. It is not produced, endorsed or certified by the Digital
Container Shipping Association or by any carrier.
