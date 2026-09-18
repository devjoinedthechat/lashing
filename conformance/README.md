# DCSA conformance

`run_conformance.py` drives DCSA's own
[Conformance Framework](https://github.com/dcsaorg/Conformance-Gateway) against lashing, with no
browser, through the JSON API its web UI uses. It tests both sides of Booking 2.0:

| Role | The framework plays | Under test |
|---|---|---|
| `carrier` | the shipper | lashing's simulated carrier (`lashing sim --manual`) |
| `shipper` | the carrier | lashing's DCSA client (`HttpCarrier`), making the calls the MCP tools make |

## Results

Last run: 2026-09-18, against Conformance-Gateway at commit `1d0bf38`. The framework offers Booking
as version 2.0.0, with its 2.0.4 schema.

| Role | Scenarios | Checks passed | Checks failed | Overall |
|---|---|---|---|---|
| Carrier (simulator) | 23 of 23, for dry, reefer and dangerous-goods cargo | 624 | 0 | CONFORMANT |
| Shipper (lashing's client) | 13 of 13 | 523 | 0 | CONFORMANT |

Booking notifications are optional in the standard. lashing polls with `GET` instead, so the
framework reports its notification checks as having no traffic, not as failures.

What the framework found, all fixed:
- **lashing's client** sent update and amendment bodies without `carrierBookingRequestReference`
  or `carrierBookingReference`, which `UpdateBooking` requires. They are now built by
  `lashing.dcsa.booking.update_body`, which keeps both.
- **The simulator** confirmed bookings without a `commoditySubReference` on each commodity.
- **The simulator** refused an amendment addressed by the request reference. The standard allows
  either reference for a `PUT`.
- **The simulator's amended view** of a confirmed booking left out the confirmed equipment,
  transport plan and cut-off times.
- **The simulator** made a request cancellation (UseCase 11) wait for the carrier, although the
  standard has no carrier step for it.

**A discrepancy in the standard itself.** The `CancelBookingRequest` descriptions say an
amendment or a confirmed booking is cancelled only "in combination with the `bookingReference`
path-property being the `carrierBookingReference`". The framework's synthetic shipper sends the
request reference instead when it has received no notification carrying the booking reference.

The simulator therefore accepts either reference. lashing's client sends the one the text asks for.

## Run it

You need Docker and uv.

```sh
git clone --depth 1 https://github.com/dcsaorg/Conformance-Gateway && cd Conformance-Gateway
docker compose up -d                       # backend on :8080, web UI on :4200; the first build takes a few minutes
cd -

uv run lashing sim --port 8401 --manual &  # the carrier decides only when the script tells it to
uv run python conformance/run_conformance.py carrier
uv run python conformance/run_conformance.py shipper
```

`--match REGEX` runs only matching scenarios, and `--verbose` lists every check, not only the
failing ones. The framework's JSON reports are written to `conformance/out/`.

The script answers each prompt the way a person would in the web UI:
- **It supplies the booking.** DCSA's example names a voyage no carrier sails, so the script asks
  for a departure date instead.
- **As the carrier,** it performs each use case through the simulator's `/_sim` controls.
- **As the shipper,** it makes the call with lashing's client.

In both roles it then marks the action completed. It counts a scenario as passed only if no
individual check failed.
