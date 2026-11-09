"""Redacted, append-only audit log (RS-9). Every decision is logged."""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .redact import redact


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditLog:
    def __init__(self, log_dir: Path, *, echo: TextIO | None = sys.stderr,
                 run_id: str | None = None):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
        self.path = self.log_dir / f"audit-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        self.echo = echo
        self.events: list[dict[str, Any]] = []

    def event(self, event: str, level: str = "info", **fields: Any) -> None:
        record = {"ts": utcnow(), "run_id": self.run_id, "level": level, "event": event}
        record.update(fields)
        record = redact(record)
        self.events.append(record)
        line = json.dumps(record, sort_keys=True, default=str)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        with os.fdopen(fd, "a") as fh:
            fh.write(line + "\n")
        if self.echo is not None:
            detail = {k: v for k, v in record.items() if k not in ("ts", "run_id", "level", "event")}
            print(f"[{record['level']}] {event} {json.dumps(detail, default=str) if detail else ''}".rstrip(),
                  file=self.echo)

    def info(self, event: str, **f: Any) -> None:
        self.event(event, "info", **f)

    def warn(self, event: str, **f: Any) -> None:
        self.event(event, "warn", **f)

    def error(self, event: str, **f: Any) -> None:
        self.event(event, "error", **f)
