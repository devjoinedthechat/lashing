"""An append-only, hash-chained record of everything lashing proposes, approves, applies or refuses.

Each line is a JSON object carrying the SHA-256 of the line before it and of itself, so editing,
reordering or deleting any entry breaks every hash after it. Appends are serialized with a file
lock, because the MCP server and the `lashing approve` command write to the same file.

Reads are incremental: the ledger keeps what it has parsed and only reads bytes appended since,
so a long-running server does not re-read its whole history on every call. A line left half
written by a crash (no trailing newline) was never acknowledged to anyone; readers skip it and the
next append removes it.

A chain cannot prove that nothing was cut off its end. `head()` gives the latest hash so it can be
anchored somewhere else (a ticket, a commit, a log shipper) when that matters.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

log = logging.getLogger(__name__)

GENESIS = "0" * 64

try:  # POSIX
    import fcntl

    def _lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

except ImportError:  # pragma: no cover - Windows

    def _lock(handle: BinaryIO) -> None:
        import msvcrt  # noqa: PLC0415

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]


def _canonical(entry: dict[str, Any]) -> bytes:
    # allow_nan=False: Infinity and NaN are not JSON, and a ledger line must be readable by anyone.
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


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
        self._entries: list[dict[str, Any]] = []
        self._offset = 0  # bytes of the file already parsed into _entries
        self._identity: tuple[int, int] | None = None  # (device, inode) of the file parsed
        self._last_line = b""  # the last complete line parsed, to notice a file rewritten in place
        self.generation = 0  # bumped whenever the cache is rebuilt, so derived indexes know to rebuild
        self._guard = threading.Lock()

    # -- reading -----------------------------------------------------------------------------------

    def _catch_up(self, handle: BinaryIO) -> None:
        """Parse whatever was appended since the last read; start over if the file was replaced."""
        stat = os.fstat(handle.fileno())
        identity = (stat.st_dev, stat.st_ino)
        if identity != self._identity or stat.st_size < self._offset or not self._tail_intact(handle):
            self._entries, self._offset, self._identity, self._last_line = [], 0, identity, b""
            self.generation += 1
        handle.seek(self._offset)
        chunk = handle.read()
        complete = chunk.rfind(b"\n") + 1  # a torn final line waits until it is finished or removed
        for line in chunk[:complete].splitlines():
            if line.strip():
                self._entries.append(json.loads(line))
                self._last_line = line + b"\n"
        self._offset += complete

    def _tail_intact(self, handle: BinaryIO) -> bool:
        if not self._last_line:
            return True
        handle.seek(self._offset - len(self._last_line))
        return handle.read(len(self._last_line)) == self._last_line

    def since(self, start: int) -> tuple[list[dict[str, Any]], int]:
        """Entries from index `start` on, and the cache generation they belong to."""
        if not self.path.exists():
            return [], self.generation
        with self._guard, self.path.open("rb") as handle:
            self._catch_up(handle)
            return self._entries[start:], self.generation

    def entries(self) -> list[dict[str, Any]]:
        """Every complete entry, in order. The list is a copy; the ledger keeps its own."""
        if not self.path.exists():
            return []
        with self._guard, self.path.open("rb") as handle:
            self._catch_up(handle)
            return list(self._entries)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.entries())

    # -- writing -----------------------------------------------------------------------------------

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
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        with self._guard, os.fdopen(descriptor, "r+b") as handle:
            _lock(handle)
            self._catch_up(handle)
            size = os.fstat(handle.fileno()).st_size
            if size > self._offset:  # a torn line from a crashed append: it was never acknowledged
                log.warning("removing an unfinished ledger line (%d bytes) left by a crash", size - self._offset)
                handle.truncate(self._offset)
            if not guard(self._entries):
                return None
            previous = self._entries[-1] if self._entries else None
            entry: dict[str, Any] = {
                "seq": (previous["seq"] + 1) if previous else 1,
                "at": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "kind": kind,
                **fields,
                "prev": previous["hash"] if previous else GENESIS,
            }
            entry["hash"] = _digest(entry)
            line = _canonical(entry) + b"\n"
            handle.seek(self._offset)
            handle.write(line)
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
            self._entries.append(entry)
            self._offset += len(line)
            self._last_line = line
        return entry

    # -- checking ----------------------------------------------------------------------------------

    def head(self) -> str:
        entries = self.entries()
        return entries[-1]["hash"] if entries else GENESIS

    def verify(self) -> Verification:
        """Re-read the file from disk, independently of what this process has cached, and check the chain."""
        if not self.path.exists():
            return Verification(True, 0, GENESIS)
        lines = self.path.read_bytes().split(b"\n")
        torn = lines.pop()  # the text after the last newline: empty unless a line is unfinished
        previous = GENESIS
        for count, raw in enumerate(lines, start=1):
            problem = _problem(raw, count, previous)
            if problem is not None:
                return Verification(False, count, previous, problem)
            previous = json.loads(raw)["hash"]
        if torn.strip():
            return Verification(False, len(lines), previous, "the last line is unfinished (a crash during an append?)")
        return Verification(True, len(lines), previous)


def _problem(raw: bytes, count: int, previous: str) -> str | None:
    """What is wrong with ledger line `count`, given the hash of the line before it."""
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError as error:
        return f"line {count} is not JSON: {error}"
    if entry.get("seq") != count:
        return f"entry {count} has seq {entry.get('seq')}"
    if entry.get("prev") != previous:
        return f"entry {count} does not follow entry {count - 1}"
    if entry.get("hash") != _digest(entry):
        return f"entry {count} was altered"
    return None
