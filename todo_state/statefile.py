"""``state.json`` cache with a ``seq`` guard and an atomic rewrite.

The cache is a projection of the log, never an input. Deleting it changes
nothing but the work done on the next read: the fold rebuilds the same bytes
(DRY, RTL-08).

Freshness rule: the cache is fresh when its ``seq`` equals the record count of
the log. ``seq`` is assigned at fold time, so the count and the folded ``seq``
are the same number for a full fold.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from . import log
from .fold import fold

CACHE_SCHEMA = 1


def dump_board(board: dict[str, Any]) -> bytes:
    """Stable bytes for a board: sorted keys, two-space indent, one trailing newline."""
    body = json.dumps(board, ensure_ascii=False, indent=2, sort_keys=True)
    return (body + "\n").encode("utf-8")


def write_cache(path: str, board: dict[str, Any]) -> None:
    """Write the cache atomically: temp file in the same directory, then rename."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(dump_board(board))
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def read_cache(path: str) -> dict[str, Any] | None:
    """Read the cache. A broken cache returns ``None`` and says so."""
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warn(f"state cache unreadable, rebuilding: {exc}")
        return None
    try:
        value = json.loads(data)
    except ValueError as exc:
        log.warn(f"state cache is not json, rebuilding: {exc}")
        return None
    if not isinstance(value, dict) or "seq" not in value:
        log.warn("state cache has no seq, rebuilding")
        return None
    return value


def load_board(
    directory: str | None = None,
    at_seq: int | None = None,
    *,
    log_path: str | None = None,
    cache_path: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return the board, folding from the log when the cache cannot be trusted.

    ``at_seq`` forces a fold and leaves the cache alone: a historical view must
    not overwrite the current one.
    """
    events_file = log_path or log.events_path(directory)
    cache_file = cache_path or log.state_path(directory)

    records: list[Any] | None = None
    if at_seq is None and use_cache:
        cached = read_cache(cache_file)
        if cached is not None and cached.get("seq") == _count(events_file):
            return cached

    try:
        records = log.read_log(events_file)
    except OSError as exc:
        log.warn(f"event log unreadable, board is empty: {exc}")
        records = []

    board = fold(records, at_seq=at_seq)
    if at_seq is None:
        write_cache(cache_file, board)
    return board


def _count(path: str) -> int:
    try:
        return log.count_records(path)
    except OSError as exc:
        log.warn(f"event log unreadable, cache treated as stale: {exc}")
        return -1
