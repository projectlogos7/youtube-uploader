"""Credential storage in the macOS Keychain (FR-2).

The refresh token and OAuth client secret are stored together as one generic
password item. Intended deployment: the uploader runs as a dedicated, restricted
macOS user (e.g. ``_ytuploader``) whose login keychain only that user can open,
so Claude, Grok, the implementer's shell and other scripts running as other
users cannot read it. See docs/RUNBOOK.md and scripts/isolation_check.sh.

Secrets are passed to ``security`` on stdin (``security -i``), never on argv,
so they do not appear in ``ps`` output.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from typing import Protocol

from .config import KeychainConfig
from .errors import AuthError


class SecretStore(Protocol):
    def read(self) -> dict | None: ...
    def write(self, secret: dict) -> None: ...
    def delete(self) -> None: ...


class MacKeychain:
    def __init__(self, cfg: KeychainConfig):
        self.cfg = cfg
        self.security = shutil.which("security") or "/usr/bin/security"

    def _kc(self) -> list[str]:
        return [self.cfg.keychain_path] if self.cfg.keychain_path else []

    def read(self) -> dict | None:
        r = subprocess.run([self.security, "find-generic-password", "-s", self.cfg.service,
                            "-a", self.cfg.account, "-w", *self._kc()],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 44:  # errSecItemNotFound
            return None
        if r.returncode != 0:
            # Do not echo stderr verbatim; it can be long and is not useful to callers.
            raise AuthError(f"keychain read denied or failed (exit {r.returncode})")
        try:
            return json.loads(r.stdout.strip())
        except json.JSONDecodeError:
            raise AuthError("keychain item is not in the expected format") from None

    def exists(self) -> bool:
        """Check for the item without reading its secret."""
        r = subprocess.run([self.security, "find-generic-password", "-s", self.cfg.service,
                            "-a", self.cfg.account, *self._kc()],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0

    def write(self, secret: dict) -> None:
        payload = json.dumps(secret, separators=(",", ":"))
        cmd = ["add-generic-password", "-U", "-s", self.cfg.service, "-a", self.cfg.account,
               "-l", "studio-uploader YouTube credential", "-w", payload, *self._kc()]
        line = " ".join(shlex.quote(c) for c in cmd) + "\n"
        r = subprocess.run([self.security, "-i"], input=line, capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or "error" in r.stderr.lower():
            raise AuthError("keychain write failed")

    def delete(self) -> None:
        subprocess.run([self.security, "delete-generic-password", "-s", self.cfg.service,
                        "-a", self.cfg.account, *self._kc()],
                       capture_output=True, text=True, timeout=30)


class MemoryStore:
    """For tests only."""

    def __init__(self, secret: dict | None = None):
        self.secret = secret

    def read(self) -> dict | None:
        return self.secret

    def exists(self) -> bool:
        return self.secret is not None

    def write(self, secret: dict) -> None:
        self.secret = dict(secret)

    def delete(self) -> None:
        self.secret = None
