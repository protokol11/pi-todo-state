"""Log tests: append atomicity, read order, corrupt-line quarantine."""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from unittest import mock

from todo_state import log

TS = "2026-10-23T10:00:00.000Z"


def ev(event_type, task_id=None, *, event_id=None, ts=TS, payload=None):
    return log.make_event(event_type, task_id=task_id, payload=payload, session_id="s1", ts=ts, event_id=event_id)


class AppendTest(unittest.TestCase):
    def test_one_line_per_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, log.LOG_NAME)
            first = ev("task_added", "t1", payload={"title": "spec E4"})
            second = ev("task_started", "t1")
            log.append_event(path, first)
            log.append_event(path, second)

            with open(path, "rb") as stream:
                raw = stream.read()
            lines = raw.decode("utf-8").splitlines()

        self.assertEqual(raw.count(b"\n"), 2)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(lines[0]), first)
        self.assertEqual(json.loads(lines[1]), second)

    def test_append_returns_written_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, log.LOG_NAME)
            event = ev("task_added", "t1", payload={"title": "x"})
            written = log.append_event(path, event)
            self.assertEqual(written, len(log.encode_event(event)))
            self.assertEqual(os.path.getsize(path), written)

    def test_append_creates_missing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "deeper", log.LOG_NAME)
            log.append_event(path, ev("task_added", "t1", payload={"title": "x"}))
            self.assertTrue(os.path.exists(path))

    def test_append_keeps_prior_bytes(self):
        """Second writer must not rewrite the first writer's line."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, log.LOG_NAME)
            log.append_event(path, ev("task_added", "t1", payload={"title": "a"}))
            with open(path, "rb") as stream:
                before = stream.read()
            log.append_event(path, ev("task_added", "t2", payload={"title": "b"}))
            with open(path, "rb") as stream:
                after = stream.read()

        self.assertTrue(after.startswith(before))
        self.assertEqual(len(after.splitlines()), 2)

    def test_no_read_modify_write_mode_in_source(self):
        """Acceptance check: the log module never opens the file for update."""
        source = inspect.getsource(log)
        for forbidden in ('"r+"', "'r+'", '"w"', "'w'", '"a+"', "O_RDWR", "O_TRUNC"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn("O_APPEND", source)


class ReadTest(unittest.TestCase):
    def test_missing_log_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(log.read_log(os.path.join(directory, "nope.jsonl")), [])
            self.assertEqual(log.count_records(os.path.join(directory, "nope.jsonl")), 0)

    def test_blank_lines_skipped_and_counts_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, log.LOG_NAME)
            first = log.encode_event(ev("task_added", "t1", payload={"title": "a"})).decode("utf-8")
            second = log.encode_event(ev("task_added", "t2", payload={"title": "b"})).decode("utf-8")
            with open(path, "wb") as stream:
                stream.write((first + "\n" + second + "\n\n").encode("utf-8"))

            records = log.read_log(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(log.count_records(path), len(records))

    def test_merge_logs_orders_by_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            early = os.path.join(directory, "early.jsonl")
            late = os.path.join(directory, "late.jsonl")
            log.append_event(early, ev("task_added", "t1", payload={"title": "a"}))
            log.append_event(late, ev("task_added", "t2", payload={"title": "b"}))
            os.utime(early, ns=(1_000_000_000, 1_000_000_000))
            os.utime(late, ns=(2_000_000_000, 2_000_000_000))

            forward = log.merge_logs([late, early])
            backward = log.merge_logs([early, late])

        self.assertEqual([record["task_id"] for record in forward], ["t1", "t2"])
        self.assertEqual(
            [record["task_id"] for record in backward],
            ["t1", "t2"],
        )

    def test_unreadable_path_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                log.read_log(directory)

    def test_env_override_sets_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {log.DIR_ENV: directory}):
                self.assertEqual(log.todo_dir(), directory)
                self.assertEqual(log.events_path(), os.path.join(directory, log.LOG_NAME))
                self.assertEqual(log.state_path(), os.path.join(directory, log.STATE_NAME))


class QuarantineTest(unittest.TestCase):
    def test_corrupt_line_quarantined_with_raw_payload(self):
        good_before = log.encode_event(ev("task_added", "t1", payload={"title": "a"})).decode("utf-8").rstrip("\n")
        corrupt = '{"event_id": "e2", "type": "task_started", "task_id": '
        good_after = log.encode_event(ev("task_started", "t1")).decode("utf-8").rstrip("\n")

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, log.LOG_NAME)
            with open(path, "wb") as stream:
                stream.write(("\n".join([good_before, corrupt, good_after]) + "\n").encode("utf-8"))
            records = log.read_log(path)

        self.assertEqual(len(records), 3)
        self.assertIsInstance(records[0], dict)
        self.assertIsInstance(records[1], log.Quarantine)
        self.assertIsInstance(records[2], dict)
        self.assertEqual(records[1].raw, corrupt)
        self.assertEqual(records[1].line_no, 2)
        self.assertIn("invalid json", records[1].reason)

    def test_non_object_line_quarantined(self):
        record = log.parse_line("[1, 2, 3]", line_no=7)
        self.assertIsInstance(record, log.Quarantine)
        self.assertIn("not a json object", record.reason)
        self.assertEqual(record.line_no, 7)

    def test_undecodable_bytes_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, log.LOG_NAME)
            with open(path, "wb") as stream:
                stream.write(b"\xff\xfe not json\n")
            records = log.read_log(path)

        self.assertEqual(len(records), 1)
        self.assertIsInstance(records[0], log.Quarantine)


if __name__ == "__main__":
    unittest.main()
