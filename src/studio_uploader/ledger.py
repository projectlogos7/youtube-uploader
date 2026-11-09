"""Append-only upload ledger (RS-3) and duplicate guard (RS-4).

One JSON object per line. Lines are only ever appended. Fields:
manifest/approval ID, file hash, channel ID, video ID, status, timestamps.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any, Iterator

from .audit import utcnow

STATUSES = {
    "started",                # session initiated
    "uploaded",               # API returned a video resource
    "verified",               # videos.list confirmed channel + private
    "reconciled",             # uncertain outcome matched to a video on the channel
    "uncertain",              # could not determine outcome; reviewer must resolve
    "resolved_not_uploaded",  # reviewer confirmed no video was created
    "failed",                 # hard stop during this job
}


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, *, approval_id: str, file_sha256: str, channel_id: str | None,
               status: str, video_id: str | None = None, **extra: Any) -> dict:
        if status not in STATUSES:
            raise ValueError(f"unknown ledger status {status!r}")
        entry = {"ts": utcnow(), "approval_id": approval_id, "file_sha256": file_sha256,
                 "channel_id": channel_id, "video_id": video_id, "status": status}
        entry.update(extra)
        line = json.dumps(entry, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return entry

    def entries(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open() as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"ledger line {n} is corrupt; refusing to continue") from exc

    def video_for_hash(self, file_sha256: str) -> dict | None:
        """RS-4: return the entry if this hash already produced a video."""
        for e in self.entries():
            if e.get("file_sha256") == file_sha256 and e.get("video_id"):
                return e
        return None

    def latest_for(self, approval_id: str) -> dict | None:
        last = None
        for e in self.entries():
            if e.get("approval_id") == approval_id:
                last = e
        return last
