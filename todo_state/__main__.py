"""CLI over the append-only board: add, start, done, block, wontfix, clear, board.

Every mutation is one appended event. The command reads the board first only to
fail loudly on a bad task id; the log append itself never depends on that read,
so a concurrent writer cannot turn the append into lost data.

Exit codes: 0 success, 2 refused input or unknown task id.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from . import __version__, log
from .fold import STATUSES
from .statefile import dump_board, load_board

TYPE_BY_COMMAND = {
    "start": "task_started",
    "done": "task_completed",
    "block": "task_blocked",
    "wontfix": "task_wontfixed",
}


def _session_id(args: argparse.Namespace) -> str:
    explicit = args.session_id or os.environ.get("TODO_STATE_SESSION_ID")
    return explicit or f"cli-{os.getpid()}"


def _directory(args: argparse.Namespace) -> str:
    return args.dir or log.todo_dir()


def _append(
    args: argparse.Namespace,
    directory: str,
    event_type: str,
    task_id: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    event = log.make_event(
        event_type,
        task_id=task_id,
        payload=payload,
        session_id=_session_id(args),
    )
    log.append_event(log.events_path(directory), event)


def _cmd_add(args: argparse.Namespace) -> int:
    title = args.title.strip()
    if not title:
        log.warn("add needs a non-empty title")
        return 2
    if args.status not in STATUSES:
        log.warn(f"unknown status: {args.status}")
        return 2

    directory = _directory(args)
    board = load_board(directory)
    taken = {task["task_id"] for task in board["tasks"]}

    task_id = args.task_id
    if task_id:
        if task_id in taken:
            log.warn(f"task id already on the board: {task_id}")
            return 2
    else:
        task_id = log.new_task_id()
        while task_id in taken:
            task_id = log.new_task_id()

    payload: dict[str, Any] = {"title": title, "status": args.status}
    if args.note:
        payload["note"] = args.note
    _append(args, directory, "task_added", task_id, payload)
    print(task_id)
    return 0


def _cmd_mutate(args: argparse.Namespace) -> int:
    directory = _directory(args)
    board = load_board(directory)
    known = {task["task_id"] for task in board["tasks"]}
    if args.task_id not in known:
        log.warn(f"unknown task id: {args.task_id}")
        return 2

    event_type = TYPE_BY_COMMAND[args.command]
    payload = {"note": args.note} if args.note else {}
    _append(args, directory, event_type, args.task_id, payload)

    print(f"{args.command} {args.task_id}")
    previous = board.get("in_progress")
    if event_type == "task_started" and previous and previous != args.task_id:
        print(f"superseded {previous}")
    return 0


def _cmd_clear(args: argparse.Namespace) -> int:
    directory = _directory(args)
    board = load_board(directory)
    count = len(board["tasks"])
    _append(args, directory, "board_cleared", None, {})
    suffix = "" if count == 1 else "s"
    print(f"cleared {count} task{suffix}")
    return 0


def _print_human(board: dict[str, Any]) -> None:
    tasks = board.get("tasks") or []
    active = board.get("in_progress") or "-"
    print(
        f"seq={board.get('seq', 0)} tasks={len(tasks)} in_progress={active} "
        f"parse_errors={board.get('parse_errors', 0)} "
        f"rejected={len(board.get('rejected') or [])} "
        f"deduped={board.get('deduped', 0)}"
    )
    for task in tasks:
        line = f"{task['status']:12} {task['task_id']:12} {task['title']}"
        if task.get("note"):
            line += f"  note: {task['note']}"
        if task.get("superseded_by"):
            line += f"  superseded_by={task['superseded_by']}"
        print(line)


def _report_problems(board: dict[str, Any]) -> None:
    """Repeat every rejection on stderr. A dropped line is never only a count."""
    for entry in board.get("rejected") or []:
        raw = entry.get("raw")
        detail = f" raw={raw!r}" if raw else ""
        log.warn(f"rejected event seq={entry.get('seq')}: {entry.get('reason')}{detail}")
    if board.get("parse_errors"):
        log.warn(f"parse_errors={board['parse_errors']}")


def _cmd_board(args: argparse.Namespace) -> int:
    board = load_board(_directory(args), at_seq=args.at_seq)
    if args.as_json:
        sys.stdout.write(dump_board(board).decode("utf-8"))
    else:
        _print_human(board)
    _report_problems(board)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo_state",
        description="Append-only todo board. State lives in .pi/todo.",
    )
    parser.add_argument("--version", action="version", version=f"todo-state {__version__}")
    parser.add_argument(
        "--dir",
        default=None,
        help="state directory (default: $TODO_STATE_DIR or .pi/todo)",
    )
    parser.add_argument("--session-id", default=None, help="session id recorded on each event")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add a task, print its id")
    add.add_argument("title")
    add.add_argument("--task-id", default=None, help="explicit task id")
    add.add_argument("--status", default="pending", help="initial status (default: pending)")
    add.add_argument("--note", default=None)
    add.set_defaults(handler=_cmd_add)

    helps = {
        "start": "move a task to in_progress, demoting the previous one",
        "done": "mark a task done",
        "block": "mark a task blocked",
        "wontfix": "mark a task wontfix",
    }
    for name, text in helps.items():
        command = sub.add_parser(name, help=text)
        command.add_argument("task_id")
        command.add_argument("--note", default=None)
        command.set_defaults(handler=_cmd_mutate)

    clear = sub.add_parser("clear", help="clear the board with one event")
    clear.set_defaults(handler=_cmd_clear)

    board = sub.add_parser("board", help="print the board")
    board.add_argument("--json", action="store_true", dest="as_json", help="print the board as json")
    board.add_argument("--at-seq", type=int, default=None, help="replay the log up to this seq")
    board.set_defaults(handler=_cmd_board)

    return parser


def _use_utf8_streams() -> None:
    """Force UTF-8 on stdout/stderr before any line is printed.

    A Windows console defaults to a legacy codepage (cp1252 here), so a single
    `→` or a Cyrillic title raised UnicodeEncodeError and took the whole CLI
    down — every todo_read/todo_write/todo_update failed on a board that
    happened to hold one such byte. The extension reads our stdout as UTF-8
    (`child.stdout.setEncoding("utf8")`), so UTF-8 is also the correct wire
    format, not merely a workaround.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # detached or already-binary stream
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # A wrapped stream may refuse; printing is still better than aborting.
            pass


def main(argv: list[str] | None = None) -> int:
    _use_utf8_streams()
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
