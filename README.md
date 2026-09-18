# lashing

An MCP server that lets AI agents book and track container shipments through the
[DCSA](https://dcsa.org) open standards, with every change validated, authorized and logged.

Lashing is what stops cargo from shifting at sea. This project does the same for agents that
act on bookings: an agent can propose any change, but nothing reaches a carrier until it
conforms to the standard, is covered by a grant or a human approval, and is recorded.

> **Status: pre-alpha.** Not yet usable end to end. The table below says what works today.

## Status

Last updated 2026-09-18.

| Area | State |
|---|---|
| DCSA Booking 2.0.5, Commercial Schedules 1.0.4 and Track & Trace 3.0.0 specs vendored at pinned commits | ✅ |
| Validation against the specs, tested on DCSA's own examples and Conformance Framework samples | ✅ |
| Booking lifecycle rules (update vs amendment, the three cancellation forms, which reference each call uses) | ✅ |
| Simulated carrier over HTTP: bookings, point-to-point schedules, tracking events, delays, scenario controls | ✅ |
| DCSA HTTP client | ⬜ |
| Plan-then-apply writes, grants, approvals, hash-chained ledger | ⬜ |
| MCP tools | ⬜ |
| Attack suite and agent evals | ⬜ |

## The simulated carrier

`lashing.sim` is a carrier you can run with no access to anyone's systems. Its network uses
real UN/LOCODEs and fictional services, vessels, IMO numbers and container numbers (all with
valid check digits), with weekly voyages from Asia to Europe, the US West Coast and the Gulf,
and a North Europe feeder for transshipments.

It serves the provider side of the three standards:

| Path | Standard |
|---|---|
| `POST /bkg/v2/bookings`, `GET/PUT/PATCH /bkg/v2/bookings/{reference}` | Booking 2.0.5 |
| `GET /cs/v1/point-to-point-routes` | Commercial Schedules 1.0.4 |
| `GET /tnt/v3/events` | Track & Trace 3.0.0 |

The booking desk behaves like a carrier's: it confirms a booking when there is a sailing with
space whose cut-off has not passed, asks for an update when cargo weight is missing or a vessel
is full, confirms or declines amendments, and declines a cancellation once the cargo has sailed.
Tracking events follow the simulator's clock: planned, then estimated when a voyage is delayed,
then actual.

Scenario controls (`/_sim/advance`, `/_sim/delay`, `/_sim/override`) move the clock, delay a
voyage, or make the carrier ask for changes, reject, decline or say anything in its feedback,
including text written to manipulate an agent. Tests check every response, errors included,
against the DCSA schemas.

## Standards

| Standard | Version | Source |
|---|---|---|
| Booking | 2.0.5 | [dcsaorg/DCSA-OpenAPI](https://github.com/dcsaorg/DCSA-OpenAPI) |
| Commercial Schedules | 1.0.4 | [dcsaorg/DCSA-OpenAPI](https://github.com/dcsaorg/DCSA-OpenAPI) |
| Track & Trace | 3.0.0 | [dcsaorg/Conformance-Gateway](https://github.com/dcsaorg/Conformance-Gateway) |

The exact commits are in [src/lashing/dcsa/specs/SOURCES.json](src/lashing/dcsa/specs/SOURCES.json).
Regenerate with `uv run python scripts/vendor_specs.py`.

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run mypy
```

## License

Apache-2.0. See [NOTICE](NOTICE) for the DCSA material this project includes.

lashing is an independent project. It is not produced, endorsed or certified by the Digital
Container Shipping Association or by any carrier.
