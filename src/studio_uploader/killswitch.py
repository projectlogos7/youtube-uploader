"""Kill switch (RS-8): a single flag file disables all live calls immediately.

It is checked before every API request, not just at start of run.
"""

from __future__ import annotations

from pathlib import Path

from .audit import utcnow
from .errors import KillSwitchEngaged


class KillSwitch:
    def __init__(self, path: Path):
        self.path = Path(path)

    def engaged(self) -> bool:
        return self.path.exists()

    def check(self) -> None:
        if self.engaged():
            raise KillSwitchEngaged(f"kill switch engaged ({self.path})")

    def engage(self, who: str, reason: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"engaged_at={utcnow()}\nby={who}\nreason={reason}\n")

    def release(self) -> None:
        if self.path.exists():
            self.path.unlink()
