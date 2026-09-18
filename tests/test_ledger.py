"""The ledger under crashes, concurrent writers and a growing history."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from lashing.ledger import Ledger
from lashing.plans import PlanBook


def test_a_torn_line_from_a_crash_is_skipped_then_removed(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append("approved", plan_id="p", by="a")
    with ledger.path.open("ab") as handle:
        handle.write(b'{"seq": 2, "kind": "appl')  # the process died mid-write
    assert [e["kind"] for e in Ledger(ledger.path).entries()] == ["approved"]
    assert not Ledger(ledger.path).verify().ok  # an operator still sees it until it is cleaned up
    Ledger(ledger.path).append("applied", plan_id="p")
    fresh = Ledger(ledger.path)
    assert [e["kind"] for e in fresh.entries()] == ["approved", "applied"]
    assert fresh.verify().ok


def test_state_files_are_private(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state" / "ledger.jsonl")
    ledger.append("proposed", plan={"id": "p"})
    assert stat.S_IMODE(os.stat(ledger.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(ledger.path.parent).st_mode) == 0o700


def test_an_existing_open_ledger_is_tightened_on_the_next_write(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(b"")
    path.chmod(0o644)
    Ledger(path).append("noted")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_second_writer_is_seen_without_rereading_everything(tmp_path: Path) -> None:
    mine, theirs = Ledger(tmp_path / "l.jsonl"), Ledger(tmp_path / "l.jsonl")
    mine.append("proposed", plan={"id": "p"})
    theirs.append("approved", plan_id="p", by="operator:alice")
    assert [e["kind"] for e in mine.entries()] == ["proposed", "approved"]
    assert mine.entries()[-1]["seq"] == 2


def test_a_file_replaced_behind_the_cache_is_reread(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.jsonl")
    ledger.append("approved", plan_id="p", by="operator:alice")
    before = ledger.generation
    ledger.path.write_text(ledger.path.read_text().replace("alice", "carol"))  # same length, edited in place
    assert ledger.entries()[0]["by"] == "operator:carol"
    assert ledger.generation == before + 1
    assert not ledger.verify().ok


def test_plan_state_stays_fast_as_the_ledger_grows(tmp_path: Path) -> None:
    book = PlanBook(Ledger(tmp_path / "l.jsonl"))
    for n in range(2000):
        book.ledger.append("noise", n=n)
    book.states()
    started = time.perf_counter()
    for _ in range(200):
        book.states()  # nothing new: must not re-parse 2,000 entries each time
    assert time.perf_counter() - started < 0.5
