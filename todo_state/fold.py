"""Deterministic fold: event records in, board out.

One implementation, no branches per caller (DRY). The model, the CLI and a
future gate all read the same projection.

Invariants held here, not by the writers:

1. ``status`` is drawn from a closed set of five values (CP-22).
2. At most one task is ``in_progress``. A second start demotes the previous
   task to ``pending`` and records ``superseded_by``.
3. An event that would create an unknown status, an unknown type, or a
   reference to a missing task is rejected into ``rejected`` with a reason. It
   never reaches ``tasks``.
4. ``seq`` is assigned at fold time from file order, so writers need no counter.
5. A repeated ``event_id`` is applied once (tool retries are normal).

The fold reads no clock. Timestamps come from the events, so the same log
yields the same board bytes on every machine and every run.
"""

from __future__ import annotations

from typing import Any, Iterable

from .log import Quarantine

SCHEMA_VERSION = 1

KNOWN_TYPES = (
    "task_added",
    "task_started",
    "task_completed",
    "task_blocked",
    "task_wontfixed",
    "board_cleared",
)

STATUSES = ("pending", "in_progress", "done", "blocked", "wontfix")

STATUS_BY_TYPE = {
    "task_started": "in_progress",
    "task_completed": "done",
    "task_blocked": "blocked",
    "task_wontfixed": "wontfix",
}

TASK_FIELDS = (
    "task_id",
    "title",
    "status",
    "created_ts",
    "updated_ts",
    "note",
    "superseded_by",
    "superseded_ts",
)


class FoldInvariantError(RuntimeError):
    """The fold produced a board that violates its own contract."""


def _check_shape(event: dict[str, Any]) -> str | None:
    """Structural checks. Returns a reason string or ``None``."""
    if not isinstance(event.get("event_id"), str) or not event["event_id"]:
        return "missing event_id"
    if not isinstance(event.get("session_id"), str) or not event["session_id"]:
        return "missing session_id"
    if not isinstance(event.get("ts"), str) or not event["ts"]:
        return "missing ts"
    event_type = event.get("type")
    if event_type not in KNOWN_TYPES:
        return f"unknown event type: {event_type!r}"
    if not isinstance(event.get("payload", {}), dict):
        return "payload is not a json object"
    task_id = event.get("task_id")
    if event_type == "board_cleared":
        if task_id not in (None, ""):
            return "board_cleared carries a task_id"
        return None
    if not isinstance(task_id, str) or not task_id.strip():
        return "missing task_id"
    return None


def _demote_others(tasks: dict[str, dict[str, Any]], keep_id: str, ts: str) -> None:
    for task_id, task in tasks.items():
        if task_id == keep_id:
            continue
        if task["status"] == "in_progress":
            task["status"] = "pending"
            task["superseded_by"] = keep_id
            task["superseded_ts"] = ts
            task["updated_ts"] = ts


def _apply(
    event: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
) -> str | None:
    """Mutate ``tasks``. Returns a rejection reason or ``None``."""
    event_type = event["type"]
    payload = event.get("payload") or {}
    ts = event["ts"]

    if event_type == "board_cleared":
        tasks.clear()
        return None

    task_id = event["task_id"]

    if event_type == "task_added":
        if task_id in tasks:
            return f"duplicate task_id: {task_id}"
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            return "task_added without title"
        status = payload.get("status", "pending")
        if status not in STATUSES:
            return f"unknown status: {status!r}"
        note = payload.get("note")
        tasks[task_id] = {
            "task_id": task_id,
            "title": title,
            "status": status,
            "created_ts": ts,
            "updated_ts": ts,
            "note": note if isinstance(note, str) and note else None,
            "superseded_by": None,
            "superseded_ts": None,
        }
        if status == "in_progress":
            _demote_others(tasks, task_id, ts)
        return None

    task = tasks.get(task_id)
    if task is None:
        return f"dangling task_id: {task_id}"

    status = STATUS_BY_TYPE[event_type]
    task["status"] = status
    task["updated_ts"] = ts
    # A restarted task is no longer the superseded one.
    if status == "in_progress":
        task["superseded_by"] = None
        task["superseded_ts"] = None
        _demote_others(tasks, task_id, ts)
    note = payload.get("note")
    if isinstance(note, str) and note:
        task["note"] = note
    return None


def _single_active(tasks: dict[str, dict[str, Any]]) -> str | None:
    active = [task_id for task_id, task in tasks.items() if task["status"] == "in_progress"]
    if len(active) > 1:
        raise FoldInvariantError(f"more than one in_progress task: {active}")
    return active[0] if active else None


def fold(
    records: Iterable[dict[str, Any] | Quarantine],
    at_seq: int | None = None,
) -> dict[str, Any]:
    """Fold records into a board.

    ``at_seq`` replays the log up to and including that ``seq``, which is how a
    caller inspects an earlier moment without a second log (RTL-09).
    """
    tasks: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    applied = 0
    deduped = 0
    parse_errors = 0
    seq = 0

    for index, record in enumerate(records, start=1):
        if at_seq is not None and index > at_seq:
            break
        seq = index

        if isinstance(record, Quarantine):
            parse_errors += 1
            rejected.append(
                {
                    "seq": seq,
                    "reason": record.reason,
                    "raw": record.raw,
                    "event": None,
                }
            )
            continue

        reason = _check_shape(record)
        if reason is not None:
            rejected.append(
                {
                    "seq": seq,
                    "reason": reason,
                    "raw": None,
                    "event": record,
                }
            )
            continue

        event_id = record["event_id"]
        if event_id in seen_ids:
            deduped += 1
            continue
        seen_ids.add(event_id)

        reason = _apply(record, tasks)
        if reason is not None:
            rejected.append(
                {
                    "seq": seq,
                    "reason": reason,
                    "raw": None,
                    "event": record,
                }
            )
            continue
        applied += 1

    # Task order is canonical, not arrival order: two writers interleaved in
    # different ways must produce the same board bytes.
    ordered = sorted(tasks.values(), key=lambda task: (task["created_ts"], task["task_id"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "applied": applied,
        "deduped": deduped,
        "parse_errors": parse_errors,
        "in_progress": _single_active(tasks),
        "tasks": [dict(task) for task in ordered],
        "rejected": rejected,
    }
