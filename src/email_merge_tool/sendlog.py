"""An append-only JSON Lines log of every send attempt.

The log is both an audit trail (who received which variant, when, and with
what Message-ID) and the mechanism behind ``resume``: recipients with a
``"sent"`` entry are skipped on the next run, so re-running after a crash or
a provider's daily limit never emails anyone twice.

Each line is flushed and ``fsync``-ed immediately after the message is
accepted by the server. The only remaining duplicate window is a crash in the
instant between the server accepting a message and the log line reaching
disk.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data import normalize_address

__all__ = ["SendLog", "read_log", "sent_addresses"]


class SendLog:
    """Append records to a JSON Lines file (one JSON object per line)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def write(self, **fields: Any) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> SendLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_log(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield the records in a send log. A truncated final line is ignored."""
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # partially written last line after a crash


def sent_addresses(path: str | Path) -> set[str]:
    """Normalized addresses that have a ``"sent"`` record in the log."""
    return {normalize_address(r["to"]) for r in read_log(path) if r.get("status") == "sent" and r.get("to")}
