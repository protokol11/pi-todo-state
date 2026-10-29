"""Cache tests: rebuild identity, seq guard, atomic rewrite, loud failures."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from todo_state import log, statefile
from todo_state.statefile import load_board

TS1 = "2026-10-23T10:00:00.000Z"
TS2 = "2026-10-23T10:00:01.000Z"
TS3 = "2026-10-23T10:00:02.000Z"


def ev(event_type, task_id=None, *, ts=TS1, payload=None):
    return log.make_event(event_type, task_id=task_id, payload=payload, session_id="s1", ts=ts)


class CacheRebuildTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = self._tmp.name
        self.events = log.events_path(self.directory)
        self.cache = log.state_path(self.directory)
        log.append_event(self.events, ev("task_added", "t1", ts=TS1, payload={"title": "spec E4"}))
        log.append_event(self.events, ev("task_started", "t1", ts=TS2))
        log.append_event(self.events, ev("task_added", "t2", ts=TS3, payload={"title": "spec E5"}))

    def tearDown(self):
        self._tmp.cleanup()

    def test_deleting_cache_reproduces_byte_identical_file(self):
        first = load_board(self.directory)
        with open(self.cache, "rb") as stream:
            first_bytes = stream.read()

        os.remove(self.cache)
        self.assertFalse(os.path.exists(self.cache))

        second = load_board(self.directory)
        with open(self.cache, "rb") as stream:
            second_bytes = stream.read()

        self.assertEqual(first, second)
        self.assertEqual(first_bytes, second_bytes)
        self.assertTrue(first_bytes.endswith(b"\n"))

    def test_cache_seq_equals_fold_seq(self):
        board = load_board(self.directory)

        self.assertEqual(board["seq"], log.count_records(self.events))
        self.assertEqual(board["seq"], 3)
        with open(self.cache, "rb") as stream:
            cached = json.loads(stream.read())
        self.assertEqual(cached["seq"], board["seq"])

    def test_fresh_cache_is_served_without_fold(self):
        load_board(self.directory)
        with mock.patch.object(statefile, "fold", side_effect=AssertionError("folded despite fresh cache")):
            board = load_board(self.directory)

        self.assertEqual(board["seq"], 3)

    def test_stale_cache_refolds(self):
        load_board(self.directory)
        log.append_event(self.events, ev("task_completed", "t2", ts=TS3))

        board = load_board(self.directory)

        self.assertEqual(board["seq"], 4)
        self.assertEqual({task["task_id"]: task["status"] for task in board["tasks"]}["t2"], "done")

    def test_corrupt_cache_is_rebuilt(self):
        with open(self.cache, "wb") as stream:
            stream.write(b"this is not json\n")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            board = load_board(self.directory)

        self.assertEqual(board["seq"], 3)
        self.assertIn("state cache", stderr.getvalue())
        with open(self.cache, "rb") as stream:
            self.assertEqual(json.loads(stream.read())["seq"], 3)

    def test_cache_without_seq_is_rebuilt(self):
        with open(self.cache, "wb") as stream:
            stream.write(b'{"tasks": []}\n')

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            board = load_board(self.directory)

        self.assertEqual(board["seq"], 3)
        self.assertIn("no seq", stderr.getvalue())

    def test_at_seq_leaves_cache_untouched(self):
        load_board(self.directory)
        with open(self.cache, "rb") as stream:
            before = stream.read()

        board = load_board(self.directory, at_seq=1)

        with open(self.cache, "rb") as stream:
            after = stream.read()
        self.assertEqual(board["seq"], 1)
        self.assertEqual(len(board["tasks"]), 1)
        self.assertEqual(board["tasks"][0]["status"], "pending")
        self.assertEqual(before, after)

    def test_write_leaves_no_temp_files(self):
        load_board(self.directory)

        names = sorted(os.listdir(self.directory))
        self.assertEqual(names, sorted([log.LOG_NAME, log.STATE_NAME]))

    def test_corrupt_line_reported_and_quarantined(self):
        with open(self.events, "ab") as stream:
            stream.write(b'{"broken": \n')

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            board = load_board(self.directory)

        self.assertEqual(board["parse_errors"], 1)
        self.assertEqual(board["rejected"][0]["raw"], '{"broken": ')
        self.assertEqual(len(board["tasks"]), 2)
        self.assertEqual(board["seq"], 4)

    def test_unreadable_log_yields_empty_board_and_warns(self):
        directory_log = os.path.join(self.directory, "as-directory")
        os.makedirs(directory_log, exist_ok=True)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            board = load_board(self.directory, log_path=directory_log)

        self.assertEqual(board["tasks"], [])
        self.assertEqual(board["seq"], 0)
        self.assertIn("event log unreadable", stderr.getvalue())

    def test_missing_log_yields_empty_board(self):
        other = os.path.join(self.directory, "empty-state")

        board = load_board(other)

        self.assertEqual(board["tasks"], [])
        self.assertEqual(board["in_progress"], None)
        self.assertEqual(board["seq"], 0)
        self.assertTrue(os.path.exists(log.state_path(other)))

    def test_dump_board_is_stable(self):
        board = load_board(self.directory)

        self.assertEqual(statefile.dump_board(board), statefile.dump_board(json.loads(statefile.dump_board(board))))
        self.assertEqual(statefile.dump_board(board), statefile.dump_board(board))


if __name__ == "__main__":
    unittest.main()
