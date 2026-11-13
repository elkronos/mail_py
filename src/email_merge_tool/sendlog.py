"""An append-only JSON Lines log of every send attempt.

The log is both an audit trail (who received which variant, when, and with
what Message-ID) and the mechanism behind ``resume``: recipients with a
``"sent"`` or ``"uncertain"`` entry are skipped on the next run, so re-running
after a crash or a provider's daily limit does not email them again. A run
refuses to reuse a log that already records sent messages unless it resumes.

Each line is flushed and ``fsync``-ed immediately after the server answers.
A message is logged ``"uncertain"`` when the connection drops after its data
was sent, because the server may have accepted it; it is not retried. A run
holds a lock on the log while it sends, so two runs cannot use one log at the
same time. One duplicate window remains: a crash between the server accepting
a message and the log line reaching disk.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from email import policy
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from .data import DataError, is_valid_address, normalize_address

__all__ = ["SendLog", "read_log", "sent_addresses"]

_WINDOWS_LOCK_OFFSET = 2**31 - 2


class SendLog:
    """Append records to a JSON Lines file (one JSON object per line)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._windows_lock = False
        if self.path.stat().st_size:
            with self.path.open("rb") as existing:
                existing.seek(-1, os.SEEK_END)
                torn = existing.read(1) != b"\n"
            if torn:  # a crash cut the last record short; keep the next one on its own line
                self._handle.write("\n")
                self._handle.flush()
                os.fsync(self._handle.fileno())

    def lock(self) -> None:
        """Lock the log file against other runs until it is closed.

        Raises:
            DataError: another run holds the lock.
        """
        fd = self._handle.fileno()
        try:
            if sys.platform == "win32":
                import msvcrt

                # Windows locks are mandatory, so lock one byte far past the
                # end of the file, which no read or write of the log touches.
                position = os.lseek(fd, 0, os.SEEK_CUR)
                os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    self._windows_lock = True
                finally:
                    os.lseek(fd, position, os.SEEK_SET)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DataError(f"send log {self.path} is in use by another run; wait for it to finish") from None

    def write(self, **fields: Any) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        if sys.platform == "win32" and self._windows_lock and not self._handle.closed:
            import msvcrt

            fd = self._handle.fileno()
            os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # closing the file releases it too
            self._windows_lock = False
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
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line cut short by a crash
            if isinstance(record, dict):
                yield record


#: Log statuses that mean the recipient may already have the message.
DELIVERED_STATUSES = frozenset({"sent", "uncertain"})


def sent_addresses(path: str | Path) -> set[str]:
    """Normalized addresses with a ``"sent"`` or ``"uncertain"`` record in the log."""
    found: set[str] = set()
    for record in read_log(path):
        if record.get("status") in DELIVERED_STATUSES and record.get("to"):
            found |= _logged_keys(str(record["to"]))
    return found


def _logged_keys(value: str) -> set[str]:
    if is_valid_address(value):
        return {normalize_address(value)}
    # Version 0.2.0 accepted some values that are now invalid, such as
    # "ann@x.org extra". Those messages went to the addresses smtplib took from
    # the To header, so those count as sent.
    try:
        header = policy.default.header_factory("To", value)
        delivered = {addr.lower() for _, addr in getaddresses([str(header)]) if addr}
    except Exception:  # noqa: BLE001 - the stdlib parser raises several types on odd input
        delivered = set()
    return delivered | {normalize_address(value)}
