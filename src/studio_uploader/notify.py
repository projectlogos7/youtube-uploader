"""Reviewer notifications (RS-7 "alert the reviewer").

Alerts are always written to ``alerts/`` and the audit log. On macOS, a
notification is also shown to whoever is logged in. Messages are redacted and
never carry tokens or session URIs. No external service is used.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Callable

from .redact import redact_text

Notifier = Callable[[str, str], None]


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def macos_notifier(enabled: bool = True) -> Notifier:
    def notify(title: str, message: str) -> None:
        if not enabled or platform.system() != "Darwin" or not shutil.which("osascript"):
            return
        script = (f'display notification "{_escape(redact_text(message))[:300]}" '
                  f'with title "{_escape(redact_text(title))[:80]}" sound name "Basso"')
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass  # notifications are best effort; the alert file is authoritative
    return notify


def null_notifier(title: str, message: str) -> None:
    pass
