"""Preflight checks (``studio-uploader doctor``).

Read-only. Makes no API call and never prints a secret. Used before activation,
before the pilot and in the review pack.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
from dataclasses import asdict, dataclass
from typing import Callable

from . import approval
from .auth import check_token_age, token_age_days
from .config import Config
from .errors import UploaderError
from .killswitch import KillSwitch
from .ledger import Ledger
from .state import StateStore


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info | skip
    detail: str = ""


def run_doctor(cfg: Config, *, store=None, which: Callable[[str], str | None] = shutil.which,
               system: str | None = None) -> list[Check]:
    system = system or platform.system()
    out: list[Check] = []

    def add(name, ok, detail="", bad="fail"):
        out.append(Check(name, "ok" if ok else bad, detail))

    add("python >= 3.11", sys.version_info >= (3, 11), platform.python_version())
    add("running on macOS", system == "Darwin", system, bad="warn")

    for tool, path in (("ffprobe", cfg.readiness.ffprobe_path), ("lsof", cfg.readiness.lsof_path)):
        add(f"{tool} available", which(path) is not None, which(path) or "not found")
    if system == "Darwin":
        add("security (Keychain CLI) available", which("security") is not None)

    # Filesystem
    if cfg.base_dir.exists():
        st = cfg.base_dir.stat()
        add("base_dir owned by current user", st.st_uid == os.getuid(), str(cfg.base_dir))
        add("base_dir not writable by group/others", not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)),
            oct(st.st_mode & 0o777))
    else:
        out.append(Check("base_dir exists", "fail", str(cfg.base_dir)))
    if cfg.state_dir.exists():
        add("state dir is owner-only (0700)", (cfg.state_dir.stat().st_mode & 0o777) == 0o700,
            oct(cfg.state_dir.stat().st_mode & 0o777))
    add("staging folder readable", cfg.staging_dir.is_dir() and os.access(cfg.staging_dir, os.R_OK | os.X_OK),
        str(cfg.staging_dir))
    add("readiness requires export marker", cfg.readiness.require_marker,
        f"marker suffix {cfg.readiness.marker_suffix!r}", bad="warn")

    # Auth and channel
    add("OAuth Desktop client ID configured", bool(cfg.oauth.client_id))
    if store is None:
        out.append(Check("credential in Keychain", "skip", "not on macOS or no store given"))
    else:
        try:
            present = store.exists()
            add("credential in Keychain", present, "item present (value not read)" if present else
                "missing: owner must run `authorize`")
            if present:
                age = token_age_days(store)
                try:
                    warning = check_token_age(age, cfg.oauth)
                    if age is None:
                        out.append(Check("refresh token age", "warn", "consent time not recorded"))
                    elif warning:
                        out.append(Check("refresh token age", "warn", warning))
                    else:
                        out.append(Check("refresh token age", "ok",
                                         f"{age:.1f} days" + (" (Testing mode: 7-day expiry)"
                                                              if cfg.oauth.testing_mode else "")))
                except UploaderError as exc:
                    out.append(Check("refresh token age", "fail", str(exc)))
        except UploaderError as exc:
            out.append(Check("credential in Keychain", "fail", str(exc)))
    if cfg.oauth.testing_mode:
        out.append(Check("OAuth app publishing status", "warn",
                         "Testing mode: weekly owner re-consent needed (PRD open question)"))
    pin = cfg.pinned_channel_id()
    add("channel pinned by owner", bool(pin), pin or "run `pin` with the owner")

    # Run state
    ks = KillSwitch(cfg.killswitch_path)
    out.append(Check("kill switch", "info", "ENGAGED" if ks.engaged() else "released"))
    try:
        act = approval.check_activation(cfg)
        used = approval.uploads_under_activation(Ledger(cfg.ledger_path), act["activation_id"])
        out.append(Check("live activation", "info",
                         f"{act['mode']} {act['activation_id']} by {act['approved_by']}, "
                         f"{len(used)}/{act['max_uploads']} used, expires {act['expires_at']}"))
    except UploaderError as exc:
        out.append(Check("live activation", "info", f"not active ({exc})"))
    states = StateStore(cfg.state_dir).all()
    unresolved = [s.approval_id for s in states if s.status in ("finalizing", "uncertain")]
    add("no unresolved uncertain uploads", not unresolved, ", ".join(unresolved) or "none")
    try:
        led = Ledger(cfg.ledger_path)
        out.append(Check("uploads today (Pacific)", "info", f"{led.starts_today()}/{cfg.daily_upload_cap}"))
    except RuntimeError as exc:
        out.append(Check("ledger readable", "fail", str(exc)))
    return out


def summarize(checks: list[Check]) -> int:
    return 1 if any(c.status == "fail" for c in checks) else 0


def as_dicts(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]


def format_checks(checks: list[Check]) -> str:
    sym = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "info", "skip": "skip"}
    return "\n".join(f"[{sym[c.status]}] {c.name}" + (f": {c.detail}" if c.detail else "") for c in checks)
