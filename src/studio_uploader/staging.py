"""Staging queue readiness checks (FR-4).

Only the configured staging folder is watched. A file is *ready* only when all
checks pass. Readiness never authorizes an upload: it only allows a draft
manifest to be prepared for the reviewer.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config

TEMP_NAME = re.compile(
    r"(^\.|^~|~$|\.(part|partial|tmp|temp|crdownload|download|swp|lock|!sync|icloud)$|"
    r"(^|[ ._-])(partial|incomplete|rendering|in[ _-]?progress)([ ._-]|$))",
    re.IGNORECASE,
)


@dataclass
class Readiness:
    path: Path
    ready: bool
    reasons: list[str] = field(default_factory=list)
    probe: dict | None = None


OpenChecker = Callable[[Path], bool | None]  # True=open, False=closed, None=unknown
Prober = Callable[[Path], dict]


def lsof_open_checker(lsof_path: str = "lsof") -> OpenChecker:
    def check(path: Path) -> bool | None:
        exe = shutil.which(lsof_path)
        if exe is None:
            return None
        # lsof exits 1 when no process has the file open.
        r = subprocess.run([exe, "-t", "--", str(path)], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return True
        if r.returncode == 1 and not r.stdout.strip():
            return False
        return None
    return check


def ffprobe_prober(ffprobe_path: str = "ffprobe") -> Prober:
    def probe(path: Path) -> dict:
        exe = shutil.which(ffprobe_path)
        if exe is None:
            raise RuntimeError("ffprobe not found")
        r = subprocess.run(
            [exe, "-v", "error", "-show_format", "-show_streams", "-of", "json", "--", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:300]}")
        return json.loads(r.stdout)
    return probe


def evaluate_probe(data: dict) -> list[str]:
    reasons = []
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        reasons.append("ffprobe: container has no valid duration")
    streams = data.get("streams") or []
    kinds = {s.get("codec_type") for s in streams}
    if "video" not in kinds:
        reasons.append("ffprobe: no video stream")
    if "audio" not in kinds:
        reasons.append("ffprobe: no audio stream")
    return reasons


def marker_path(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def check_ready(path: Path, cfg: Config, *, sleep: Callable[[float], None] = time.sleep,
                now: Callable[[], float] = time.time,
                open_checker: OpenChecker | None = None,
                prober: Prober | None = None) -> Readiness:
    rc = cfg.readiness
    path = Path(path)
    reasons: list[str] = []
    open_checker = open_checker or lsof_open_checker(rc.lsof_path)
    prober = prober or ffprobe_prober(rc.ffprobe_path)

    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return Readiness(path, False, ["file does not exist"])
    if cfg.staging_dir.resolve() not in resolved.parents:
        return Readiness(path, False, ["file is outside the approved staging folder"])
    if path.is_symlink() or not resolved.is_file():
        return Readiness(path, False, ["not a regular file (symlinks are not accepted)"])

    if TEMP_NAME.search(path.name):
        reasons.append("temporary or partial file name")
    if path.suffix.lower() not in rc.video_extensions:
        reasons.append(f"extension {path.suffix!r} not in allowed list")

    st = path.stat()
    if st.st_size == 0:
        reasons.append("file is empty")

    if rc.require_marker and not marker_path(path, rc.marker_suffix).exists():
        reasons.append(f"export-complete marker {path.name + rc.marker_suffix!r} not found")

    if now() - st.st_mtime < rc.min_age_s:
        reasons.append(f"modified less than {rc.min_age_s:.0f}s ago")

    if reasons:
        return Readiness(path, False, reasons)

    # Size and mtime must be stable across repeated checks.
    last = (st.st_size, st.st_mtime_ns)
    for _ in range(max(rc.stable_checks - 1, 1)):
        sleep(rc.stable_interval_s)
        s2 = path.stat()
        cur = (s2.st_size, s2.st_mtime_ns)
        if cur != last:
            return Readiness(path, False, ["size or mtime changed between checks (still being written)"])
        last = cur

    is_open = open_checker(path)
    if is_open is True:
        return Readiness(path, False, ["file is open by another process"])
    if is_open is None:
        return Readiness(path, False, ["could not determine whether file is open (lsof unavailable); failing closed"])

    try:
        data = prober(path)
    except Exception as exc:  # noqa: BLE001 - any probe failure means not ready
        return Readiness(path, False, [f"ffprobe could not read container: {exc}"])
    probe_reasons = evaluate_probe(data)
    if probe_reasons:
        return Readiness(path, False, probe_reasons, data)

    return Readiness(path, True, [], data)


def candidates(cfg: Config) -> list[Path]:
    """Files directly inside the staging folder (no recursion, no other folders)."""
    if not cfg.staging_dir.is_dir():
        return []
    out = []
    for p in sorted(cfg.staging_dir.iterdir()):
        if p.name.endswith(cfg.readiness.marker_suffix):
            continue
        if p.is_file() or p.is_symlink():
            out.append(p)
    return out
