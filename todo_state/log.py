"""Append-only event log for the todo board.

Writers never touch earlier bytes. The only write path here is
``os.O_APPEND``, so two sessions can append to one file without a lock and
without a read-modify-write cycle (PS-02, CP-22).

Truth is rebuilt by :mod:`todo_state.fold`. The log itself carries no state a
second writer could clobber: events are facts about the past, not fields.

A line the parser cannot read is never dropped. :func:`read_log` returns it as
a :class:`Quarantine` record and the fold reports it (PS-FIXER-03).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

LOG_NAME = "events.jsonl"
STATE_NAME = "state.json"
DIR_ENV = "TODO_STATE_DIR"
DEFAULT_DIR = os.path.join(".pi", "todo")


@dataclass(frozen=True)
class Quarantine:
    """A log line the parser could not turn into an event.

    ``raw`` is the line exactly as it sits in the file. The fold copies it into
    the board's ``rejected`` list, so the bad bytes stay reachable (RTL-09).
    """

    raw: str
    line_no: int
    reason: str


def warn(message: str) -> None:
    """Report a failure on stderr. Silent skipping is a bug, not a policy."""
    print(f"todo-state: {message}", file=sys.stderr)


def todo_dir() -> str:
    """State directory: ``TODO_STATE_DIR`` or the per-project default."""
    return os.environ.get(DIR_ENV) or DEFAULT_DIR


def events_path(directory: str | None = None) -> str:
    return os.path.join(directory or todo_dir(), LOG_NAME)


def state_path(directory: str | None = None) -> str:
    return os.path.join(directory or todo_dir(), STATE_NAME)


def now_ts() -> str:
    """UTC timestamp with millisecond precision, ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_event_id() -> str:
    return uuid.uuid4().hex


def new_task_id() -> str:
    return uuid.uuid4().hex[:8]


def make_event(
    event_type: str,
    *,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
    session_id: str = "cli",
    ts: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build one event.

    ``seq`` is absent on purpose: the fold assigns it, because writers do not
    coordinate and a write-time counter would need a lock.
    """
    return {
        "event_id": event_id or new_event_id(),
        "ts": ts or now_ts(),
        "session_id": session_id,
        "type": event_type,
        "task_id": task_id,
        "payload": dict(payload or {}),
    }


def encode_event(event: dict[str, Any]) -> bytes:
    """One event is one line: sorted keys, compact separators, trailing newline."""
    body = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (body + "\n").encode("utf-8")


def append_event(path: str, event: dict[str, Any]) -> int:
    """Append one event line. Returns the byte count written.

    O_APPEND makes each ``os.write`` land at the current end of file, so two
    processes interleave whole lines instead of overwriting each other. A short
    write is the one case where bytes could split across writers; the loop
    keeps the line contiguous by re-appending the remainder at the new end.
    """
    data = encode_event(event)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # O_BINARY keeps the byte count honest on Windows: a text-mode handle would
    # turn every "\n" into "\r\n" and shift the offsets the fold reads.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    handle = os.open(path, flags, 0o644)
    try:
        written = 0
        while written < len(data):
            chunk = os.write(handle, data[written:])
            if chunk <= 0:
                raise OSError(f"short write on {path}")
            written += chunk
    finally:
        os.close(handle)
    return written


def parse_line(raw: str, line_no: int = 0) -> dict[str, Any] | Quarantine:
    """Parse one line into an event or quarantine it with its raw text."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Quarantine(raw=raw, line_no=line_no, reason=f"invalid json: {exc.msg}")
    if not isinstance(value, dict):
        return Quarantine(raw=raw, line_no=line_no, reason="event is not a json object")
    return value


def read_log(path: str) -> list[dict[str, Any] | Quarantine]:
    """Read every record in file order.

    A missing file is an empty log, not an error. Any other OS failure
    propagates: the caller decides whether the board can be empty and must say
    so loudly.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any] | Quarantine] = []
    for line_no, raw_bytes in enumerate(data.split(b"\n"), start=1):
        raw = raw_bytes.decode("utf-8", errors="replace")
        if not raw.strip():
            continue
        records.append(parse_line(raw, line_no))
    return records


def count_records(path: str) -> int:
    """Count log records without parsing them.

    The cache guard compares this number with the cached ``seq``. It must match
    :func:`read_log`, so both skip blank lines.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return 0
    return sum(1 for line in data.split(b"\n") if line.strip())


def merge_logs(paths: Iterable[str]) -> list[dict[str, Any] | Quarantine]:
    """Concatenate logs ordered by file mtime, then by line order.

    Two sessions that never saw each other's writes leave two logs. Merging by
    mtime recovers a plausible global order; the fold stays the single judge of
    what that order means.
    """
    ordered = sorted(paths, key=lambda item: os.stat(item).st_mtime_ns)
    merged: list[dict[str, Any] | Quarantine] = []
    for path in ordered:
        merged.extend(read_log(path))
    return merged
