"""Fold tests: transitions, single-active invariant, idempotency, interleaving."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from todo_state import log
from todo_state.fold import FoldInvariantError, fold, _single_active
from todo_state.log import Quarantine

TS1 = "2026-10-23T10:00:00.000Z"
TS2 = "2026-10-23T10:00:01.000Z"
TS3 = "2026-10-23T10:00:02.000Z"
TS4 = "2026-10-23T10:00:03.000Z"
TS5 = "2026-10-23T10:00:04.000Z"


def ev(event_type, task_id=None, *, event_id=None, ts=TS1, payload=None, session="s1"):
    return log.make_event(
        event_type,
        task_id=task_id,
        payload=payload,
        session_id=session,
        ts=ts,
        event_id=event_id,
    )


def added(task_id, title, *, event_id=None, ts=TS1, session="s1"):
    return ev("task_added", task_id, event_id=event_id, ts=ts, payload={"title": title}, session=session)


def interleavings(left, right):
    """Every merge of two ordered lists that preserves both internal orders."""
    if not left:
        yield list(right)
        return
    if not right:
        yield list(left)
        return
    for rest in interleavings(left[1:], right):
        yield [left[0]] + rest
    for rest in interleavings(left, right[1:]):
        yield [right[0]] + rest


class FoldTransitionsTest(unittest.TestCase):
    def test_add_then_start(self):
        board = fold([added("t1", "spec E4"), ev("task_started", "t1", ts=TS2)])

        self.assertEqual(board["seq"], 2)
        self.assertEqual(board["applied"], 2)
        self.assertEqual(board["rejected"], [])
        self.assertEqual(board["in_progress"], "t1")
        self.assertEqual(board["tasks"][0]["status"], "in_progress")
        self.assertEqual(board["tasks"][0]["title"], "spec E4")

    def test_closed_status_set_per_event_type(self):
        cases = {
            "task_completed": "done",
            "task_blocked": "blocked",
            "task_wontfixed": "wontfix",
        }
        for event_type, status in cases.items():
            with self.subTest(event_type=event_type):
                board = fold([added("t1", "x"), ev(event_type, "t1", ts=TS2)])
                self.assertEqual(board["tasks"][0]["status"], status)
                self.assertIsNone(board["in_progress"])
                self.assertEqual(board["rejected"], [])

    def test_second_start_demotes_previous(self):
        board = fold(
            [
                added("t1", "first", ts=TS1),
                added("t2", "second", ts=TS2),
                ev("task_started", "t1", ts=TS3),
                ev("task_started", "t2", ts=TS4),
            ]
        )

        tasks = {task["task_id"]: task for task in board["tasks"]}
        self.assertEqual(tasks["t1"]["status"], "pending")
        self.assertEqual(tasks["t1"]["superseded_by"], "t2")
        self.assertEqual(tasks["t1"]["superseded_ts"], TS4)
        self.assertEqual(tasks["t2"]["status"], "in_progress")
        self.assertIsNone(tasks["t2"]["superseded_by"])
        self.assertEqual(board["in_progress"], "t2")
        self.assertEqual(board["rejected"], [])

    def test_restarted_task_clears_supersede_note(self):
        board = fold(
            [
                added("t1", "first"),
                added("t2", "second"),
                ev("task_started", "t1", ts=TS2),
                ev("task_started", "t2", ts=TS3),
                ev("task_started", "t1", ts=TS4),
            ]
        )

        tasks = {task["task_id"]: task for task in board["tasks"]}
        self.assertEqual(tasks["t1"]["status"], "in_progress")
        self.assertIsNone(tasks["t1"]["superseded_by"])
        self.assertEqual(tasks["t2"]["status"], "pending")
        self.assertEqual(board["in_progress"], "t1")

    def test_two_adds_with_in_progress_status_keep_one_active(self):
        board = fold(
            [
                ev("task_added", "t1", ts=TS1, payload={"title": "a", "status": "in_progress"}),
                ev("task_added", "t2", ts=TS2, payload={"title": "b", "status": "in_progress"}),
            ]
        )

        self.assertEqual(board["in_progress"], "t2")
        self.assertEqual(board["tasks"][0]["status"], "pending")
        self.assertEqual(board["tasks"][0]["superseded_by"], "t2")

    def test_board_cleared_empties_board_and_history_stays(self):
        board = fold(
            [
                added("t1", "old", ts=TS1),
                added("t2", "old too", ts=TS2),
                ev("board_cleared", None, ts=TS3),
                added("t3", "new", ts=TS4),
            ]
        )

        self.assertEqual([task["task_id"] for task in board["tasks"]], ["t3"])
        self.assertEqual(board["seq"], 4)
        self.assertEqual(board["applied"], 4)

    def test_note_lands_on_task(self):
        board = fold([added("t1", "x"), ev("task_blocked", "t1", ts=TS2, payload={"note": "waiting on E5"})])

        self.assertEqual(board["tasks"][0]["note"], "waiting on E5")
        self.assertEqual(board["tasks"][0]["status"], "blocked")


class FoldRejectionTest(unittest.TestCase):
    def test_dangling_task_id_rejected(self):
        board = fold([ev("task_started", "ghost")])

        self.assertEqual(board["tasks"], [])
        self.assertEqual(len(board["rejected"]), 1)
        self.assertIn("dangling task_id", board["rejected"][0]["reason"])
        self.assertEqual(board["rejected"][0]["seq"], 1)

    def test_unknown_event_type_rejected(self):
        board = fold([{"event_id": "e1", "ts": TS1, "session_id": "s1", "type": "task_exploded", "task_id": "t1", "payload": {}}])

        self.assertEqual(board["tasks"], [])
        self.assertIn("unknown event type", board["rejected"][0]["reason"])

    def test_unknown_status_rejected(self):
        board = fold([ev("task_added", "t1", payload={"title": "x", "status": "sorta_done"})])

        self.assertEqual(board["tasks"], [])
        self.assertIn("unknown status", board["rejected"][0]["reason"])

    def test_duplicate_task_id_rejected(self):
        board = fold([added("t1", "first"), added("t1", "second", ts=TS2)])

        self.assertEqual(len(board["tasks"]), 1)
        self.assertEqual(board["tasks"][0]["title"], "first")
        self.assertIn("duplicate task_id", board["rejected"][0]["reason"])

    def test_missing_fields_rejected(self):
        broken = [
            {"ts": TS1, "session_id": "s1", "type": "task_added", "task_id": "t1", "payload": {"title": "x"}},
            {"event_id": "e1", "ts": TS1, "session_id": "s1", "type": "task_added", "payload": {"title": "x"}},
            {"event_id": "e2", "ts": TS1, "session_id": "s1", "type": "task_added", "task_id": "t2"},
        ]
        board = fold(broken)

        self.assertEqual(board["tasks"], [])
        self.assertEqual(len(board["rejected"]), 3)
        self.assertEqual(board["applied"], 0)

    def test_corrupt_record_reported_as_parse_error(self):
        board = fold(
            [
                added("t1", "kept"),
                Quarantine(raw='{"event_id": "e9", "ty', line_no=2, reason="invalid json: Expecting value"),
            ]
        )

        self.assertEqual(len(board["tasks"]), 1)
        self.assertEqual(board["parse_errors"], 1)
        self.assertEqual(board["rejected"][0]["raw"], '{"event_id": "e9", "ty')
        self.assertEqual(board["rejected"][0]["seq"], 2)


class FoldIdempotencyTest(unittest.TestCase):
    def test_retried_write_deduped_by_event_id(self):
        start = ev("task_started", "t1", event_id="retry-me", ts=TS2)
        board = fold([added("t1", "x"), start, start])

        self.assertEqual(board["applied"], 2)
        self.assertEqual(board["deduped"], 1)
        self.assertEqual(board["seq"], 3)
        self.assertEqual(board["tasks"][0]["status"], "in_progress")

    def test_refold_is_stable(self):
        records = [
            added("t1", "a"),
            added("t2", "b", ts=TS2),
            ev("task_started", "t1", ts=TS3),
            ev("task_completed", "t1", ts=TS4),
        ]

        first = fold(records)
        second = fold(records)
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_fold_from_file_matches_fold_from_records(self):
        records = [
            added("t1", "a"),
            ev("task_started", "t1", ts=TS2),
            ev("task_blocked", "t1", ts=TS3, payload={"note": "stuck"}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, log.LOG_NAME)
            for record in records:
                log.append_event(path, record)
            from_file = fold(log.read_log(path))

        self.assertEqual(from_file, fold(records))

    def test_replay_to_at_seq(self):
        records = [
            added("t1", "a", ts=TS1),
            ev("task_started", "t1", ts=TS2),
            added("t2", "b", ts=TS3),
            ev("task_completed", "t2", ts=TS4),
        ]

        self.assertEqual(fold(records, at_seq=1)["tasks"][0]["status"], "pending")
        self.assertEqual(fold(records, at_seq=2)["in_progress"], "t1")
        self.assertEqual(fold(records, at_seq=2)["seq"], 2)
        self.assertEqual(len(fold(records, at_seq=3)["tasks"]), 2)
        self.assertEqual(fold(records, at_seq=99)["tasks"][1]["status"], "done")


class FoldConcurrencyTest(unittest.TestCase):
    def test_two_writers_interleaved_converge(self):
        writer_a = [
            added("t1", "A task one", ts=TS1, session="a"),
            ev("task_started", "t1", ts=TS2, session="a"),
            added("t2", "A task two", ts=TS3, session="a"),
        ]
        writer_b = [
            added("t3", "B task one", ts=TS1, session="b"),
            ev("task_completed", "t3", ts=TS2, session="b"),
            added("t4", "B task two", ts=TS3, session="b"),
        ]

        boards = []
        for merged in interleavings(writer_a, writer_b):
            self.assertEqual(len(merged), 6)
            boards.append(json.dumps(fold(merged), sort_keys=True))

        self.assertEqual(len(boards), 20)
        self.assertEqual(len(set(boards)), 1, "interleaved writers diverged")

        board = fold(writer_a + writer_b)
        statuses = {task["task_id"]: task["status"] for task in board["tasks"]}
        self.assertEqual(
            statuses,
            {"t1": "in_progress", "t2": "pending", "t3": "done", "t4": "pending"},
        )
        self.assertEqual(board["seq"], 6)

    def test_two_writers_interleave_on_one_log(self):
        writer_a = [added("t1", "a", ts=TS1, session="a"), ev("task_started", "t1", ts=TS3, session="a")]
        writer_b = [added("t2", "b", ts=TS2, session="b"), ev("task_started", "t2", ts=TS4, session="b")]

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, log.LOG_NAME)
            for first, second in zip(writer_a, writer_b):
                log.append_event(path, first)
                log.append_event(path, second)
            board = fold(log.read_log(path))

        self.assertEqual([task["task_id"] for task in board["tasks"]], ["t1", "t2"])
        self.assertEqual(board["in_progress"], "t2")
        self.assertEqual(board["parse_errors"], 0)

    def test_merged_logs_by_mtime_converge(self):
        log_a = [added("t1", "a", ts=TS1, session="a"), ev("task_started", "t1", ts=TS3, session="a")]
        log_b = [added("t3", "b", ts=TS2, session="b"), ev("task_completed", "t3", ts=TS4, session="b")]

        with tempfile.TemporaryDirectory() as directory:
            path_a = os.path.join(directory, "a.jsonl")
            path_b = os.path.join(directory, "b.jsonl")
            for record in log_a:
                log.append_event(path_a, record)
            for record in log_b:
                log.append_event(path_b, record)

            os.utime(path_a, ns=(1_000_000_000, 1_000_000_000))
            os.utime(path_b, ns=(2_000_000_000, 2_000_000_000))
            first = json.dumps(fold(log.merge_logs([path_a, path_b])), sort_keys=True)

            os.utime(path_a, ns=(3_000_000_000, 3_000_000_000))
            os.utime(path_b, ns=(2_000_000_000, 2_000_000_000))
            second = json.dumps(fold(log.merge_logs([path_a, path_b])), sort_keys=True)

        self.assertEqual(first, second, "mtime order changed the board")
        board = json.loads(first)
        self.assertEqual(board["applied"], 4)
        self.assertEqual(
            {task["task_id"]: task["status"] for task in board["tasks"]},
            {"t1": "in_progress", "t3": "done"},
        )


class FoldInvariantTest(unittest.TestCase):
    def test_single_active_guard_raises(self):
        tasks = {
            "t1": {"status": "in_progress"},
            "t2": {"status": "in_progress"},
        }
        with self.assertRaises(FoldInvariantError):
            _single_active(tasks)

    def test_single_active_guard_returns_one(self):
        self.assertEqual(_single_active({"t1": {"status": "in_progress"}}), "t1")
        self.assertIsNone(_single_active({"t1": {"status": "pending"}}))


if __name__ == "__main__":
    unittest.main()
