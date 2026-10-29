"""CLI encoding regression: a board line with `→` or Cyrillic must not kill the run.

Windows consoles default to a legacy codepage, so `print()` raised
UnicodeEncodeError and every todo_read/todo_write/todo_update failed on a board
that held one such byte. The CLI now forces UTF-8 on its streams; these tests pin
that by running it with PYTHONIOENCODING=cp1252, which is what the console
reported at the time of the incident.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXTENSION_DIR = Path(__file__).resolve().parents[1]
TOKEN = "починить → доску"


def run(args: list[str], state_dir: str) -> subprocess.CompletedProcess[bytes]:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        "TODO_STATE_DIR": state_dir,
        "PYTHONPATH": str(EXTENSION_DIR),
    }
    return subprocess.run(
        [sys.executable, "-m", "todo_state", "--dir", state_dir, *args],
        cwd=str(EXTENSION_DIR),
        env=env,
        capture_output=True,
    )


class CliEncodingTest(unittest.TestCase):
    def test_add_and_json_board_survive_a_legacy_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            add = run(["add", TOKEN], tmp)
            self.assertEqual(add.returncode, 0, add.stderr.decode("utf-8", "replace"))
            self.assertTrue(add.stdout.decode("utf-8").strip(), "the task id is printed on add")

            board = run(["board", "--json"], tmp)
            self.assertEqual(board.returncode, 0, board.stderr.decode("utf-8", "replace"))
            data = json.loads(board.stdout.decode("utf-8"))
            self.assertEqual(data["tasks"][0]["title"], TOKEN, "the title round-trips byte-identical")

    def test_human_board_renders_the_same_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run(["add", TOKEN], tmp).returncode, 0)
            board = run(["board"], tmp)
            self.assertEqual(board.returncode, 0, board.stderr.decode("utf-8", "replace"))
            self.assertIn(TOKEN, board.stdout.decode("utf-8"))

    def test_mutations_still_work_with_non_ascii_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_id = run(["add", TOKEN], tmp).stdout.decode("utf-8").splitlines()[0]
            for command in ("start", "done"):
                result = run([command, task_id], tmp)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            board = run(["board", "--json"], tmp)
            self.assertEqual(board.returncode, 0, board.stderr.decode("utf-8", "replace"))
            self.assertEqual(json.loads(board.stdout.decode("utf-8"))["tasks"][0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
