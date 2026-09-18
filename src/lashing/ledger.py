"""An append-only, hash-chained record of everything lashing proposes, approves, applies or refuses.

Each line is a JSON object carrying the SHA-256 of the line before it and of itself, so editing,
reordering or deleting any entry breaks every hash after it. Appends are serialized with a file
lock, because the MCP server and the `lashing approve` command write to the same file.

A chain cannot prove that nothing was cut off its end. `head()` gives the latest hash so it can be
anchored somewhere else (a ticket, a commit, a log shipper) when that matters.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64

try:  # POSIX
    import fcntl

    def _lock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

except ImportError:  # pragma: no cover - Windows

    def _lock(handle: Any) -> None:
        import msvcrt  # noqa: PLC0415

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]


def _canonical(entry: dict[str, Any]) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(entry: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in entry.items() if k != "hash"})).hexdigest()


@dataclass(frozen=True)
class Verification:
    ok: bool
    entries: int
    head: str
    problem: str | None = None


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        entry = self.append_if(lambda _: True, kind, **fields)
        if entry is None:  # pragma: no cover - the guard above always passes
            raise RuntimeError("unconditional append was refused")
        return entry

    def append_if(
        self,
        guard: Callable[[list[dict[str, Any]]], bool],
        kind: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Append only if `guard` accepts the entries so far, deciding and writing under one lock.

        This is what makes "apply a plan at most once" hold across processes.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            _lock(handle)
            handle.seek(0)
            existing = [json.loads(line) for line in handle if line.strip()]
            if not guard(existing):
                return None
            previous = existing[-1] if existing else None
            entry: dict[str, Any] = {
                "seq": (previous["seq"] + 1) if previous else 1,
                "at": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "kind": kind,
                **fields,
                "prev": previous["hash"] if previous else GENESIS,
            }
            entry["hash"] = _digest(entry)
            handle.seek(0, os.SEEK_END)
            handle.write(_canonical(entry).decode() + "\n")
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        return entry

    def head(self) -> str:
        last = GENESIS
        for entry in self.entries():
            last = entry["hash"]
        return last

    def verify(self) -> Verification:
        previous, count = GENESIS, 0
        try:
            for count, entry in enumerate(self.entries(), start=1):
                if entry.get("seq") != count:
                    return Verification(False, count, previous, f"entry {count} has seq {entry.get('seq')}")
                if entry.get("prev") != previous:
                    return Verification(False, count, previous, f"entry {count} does not follow entry {count - 1}")
                if entry.get("hash") != _digest(entry):
                    return Verification(False, count, previous, f"entry {count} was altered")
                previous = entry["hash"]
        except json.JSONDecodeError as error:
            return Verification(False, count + 1, previous, f"line {count + 1} is not JSON: {error}")
        return Verification(True, count, previous)
