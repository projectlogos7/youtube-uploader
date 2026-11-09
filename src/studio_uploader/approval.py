"""Approval gates (FR-5, FR-6, approval levels).

Gate 1 (manifest approval, per video): ``approve`` freezes the exact file into a
read-only location, stamps the manifest with the reviewer's identity and writes
an approval record binding the manifest digest to the file hash. Any later
change to either voids the approval.

Gate 2 (live approval, per video): ``release`` records the reviewer's go-ahead
for one approved job, bound to the same manifest digest.

Activation (Level 3): ``activate`` records the reviewer's enabling of live mode.
Without it, ``--live`` refuses to run.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import manifest as mf
from .audit import utcnow
from .config import Config
from .errors import (ApprovalVoid, HashMismatch, LiveModeNotActivated,
                     ManifestInvalid, NotReleased)
from .fsutil import atomic_write_json, freeze_copy, make_read_only, sha256_file, sha256_json
from .staging import Readiness


@dataclass
class Job:
    approval_id: str
    dir: Path
    manifest: dict
    record: dict
    video_path: Path

    @property
    def manifest_sha256(self) -> str:
        return sha256_json(self.manifest)


def new_approval_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + secrets.token_hex(6)


# ---- Draft -----------------------------------------------------------------

def create_draft(cfg: Config, readiness: Readiness) -> Path:
    if not readiness.ready:
        raise ManifestInvalid("file is not ready: " + "; ".join(readiness.reasons))
    pinned = cfg.pinned_channel_id()
    if not pinned:
        raise ManifestInvalid("no pinned channel; run `studio-uploader pin` first")
    file_hash = sha256_file(readiness.path)
    draft = mf.draft_template(file_hash, pinned, readiness.path.name)
    cfg.drafts_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.drafts_dir / f"{readiness.path.stem}.{file_hash[:12]}.draft.json"
    if out.exists():
        raise ManifestInvalid(f"draft already exists: {out}")
    atomic_write_json(out, draft, mode=0o640)
    return out


# ---- Gate 1: manifest approval ---------------------------------------------

def approve(cfg: Config, draft_path: Path, reviewer: str, *, staged_path: Path | None = None) -> Job:
    if not reviewer.strip():
        raise ManifestInvalid("reviewer identity is required")
    pinned = cfg.pinned_channel_id()
    if not pinned:
        raise ManifestInvalid("no pinned channel")
    draft = mf.load(draft_path)
    errs = mf.validate(draft, draft=True, pinned_channel_id=pinned)
    if errs:
        raise ManifestInvalid("; ".join(errs))

    src = staged_path or (cfg.staging_dir / draft["source_filename"])
    if cfg.staging_dir.resolve() not in src.resolve().parents:
        raise ManifestInvalid("source file is outside the staging folder")
    if sha256_file(src) != draft["file_sha256"]:
        raise HashMismatch("staged file does not match the draft's file_sha256")

    approval_id = new_approval_id()
    job_dir = cfg.frozen_dir / approval_id
    job_dir.mkdir(parents=True, exist_ok=False)
    video_path = job_dir / ("video" + src.suffix.lower())
    frozen_hash = freeze_copy(src, video_path)
    if frozen_hash != draft["file_sha256"]:
        video_path.chmod(0o600)
        video_path.unlink()
        raise HashMismatch("frozen copy hash differs from approved hash; nothing approved")

    final = dict(draft)
    final["approved_by"] = reviewer.strip()
    final["approved_at"] = utcnow()
    final["approval_id"] = approval_id
    errs = mf.validate(final, pinned_channel_id=pinned)
    if errs:
        raise ManifestInvalid("; ".join(errs))

    record = {
        "approval_id": approval_id,
        "manifest_sha256": sha256_json(final),
        "file_sha256": final["file_sha256"],
        "channel_id": final["channel_id"],
        "approved_by": final["approved_by"],
        "approved_at": final["approved_at"],
        "video_filename": video_path.name,
    }
    atomic_write_json(job_dir / "manifest.json", final, mode=0o440)
    atomic_write_json(job_dir / "approval.json", record, mode=0o440)
    make_read_only(job_dir / "manifest.json")
    make_read_only(job_dir / "approval.json")
    os.chmod(job_dir, 0o550)
    draft_path.unlink()
    return load_job(cfg, approval_id)


def load_job(cfg: Config, approval_id: str) -> Job:
    d = cfg.frozen_dir / approval_id
    try:
        m = json.loads((d / "manifest.json").read_text())
        rec = json.loads((d / "approval.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ApprovalVoid(f"approval {approval_id} incomplete or unreadable: {exc}") from exc
    return Job(approval_id, d, m, rec, d / rec.get("video_filename", "missing"))


def list_jobs(cfg: Config) -> list[str]:
    if not cfg.frozen_dir.exists():
        return []
    return sorted(p.name for p in cfg.frozen_dir.iterdir() if p.is_dir() and not p.name.startswith("."))


def verify_job(job: Job, pinned_channel_id: str, *, rehash: bool = True) -> None:
    """Raise if the approval no longer holds. Called immediately before upload."""
    errs = mf.validate(job.manifest, pinned_channel_id=pinned_channel_id)
    if errs:
        raise ManifestInvalid("; ".join(errs))
    rec = job.record
    if rec.get("approval_id") != job.approval_id or job.manifest.get("approval_id") != job.approval_id:
        raise ApprovalVoid("approval_id mismatch between folder, manifest and record")
    if job.manifest_sha256 != rec.get("manifest_sha256"):
        raise ApprovalVoid("manifest changed after approval")
    for f in ("file_sha256", "channel_id", "approved_by", "approved_at"):
        if job.manifest.get(f) != rec.get(f):
            raise ApprovalVoid(f"approval record field {f!r} does not match manifest")
    if rehash:
        if not job.video_path.is_file():
            raise HashMismatch("frozen file missing")
        if sha256_file(job.video_path) != job.manifest["file_sha256"]:
            raise HashMismatch("frozen file changed after approval")


# ---- Gate 2: per-video live approval ----------------------------------------

def release(cfg: Config, approval_id: str, reviewer: str) -> Path:
    job = load_job(cfg, approval_id)
    pinned = cfg.pinned_channel_id() or ""
    verify_job(job, pinned)
    rec = {"approval_id": approval_id, "manifest_sha256": job.manifest_sha256,
           "file_sha256": job.manifest["file_sha256"], "released_by": reviewer.strip(),
           "released_at": utcnow()}
    if not rec["released_by"]:
        raise NotReleased("reviewer identity is required")
    out = cfg.releases_dir / f"{approval_id}.json"
    atomic_write_json(out, rec, mode=0o440)
    return out


def check_released(cfg: Config, job: Job) -> dict:
    p = cfg.releases_dir / f"{job.approval_id}.json"
    if not p.exists():
        raise NotReleased("reviewer has not released this video for live upload")
    rec = json.loads(p.read_text())
    if rec.get("manifest_sha256") != job.manifest_sha256 or rec.get("file_sha256") != job.manifest["file_sha256"]:
        raise ApprovalVoid("release record does not match the approved manifest")
    if not rec.get("released_by"):
        raise NotReleased("release record has no reviewer")
    return rec


# ---- Level 3: activation ----------------------------------------------------

def activate(cfg: Config, reviewer: str, note: str = "") -> None:
    if not reviewer.strip():
        raise LiveModeNotActivated("reviewer identity is required")
    atomic_write_json(cfg.activation_path, {"enabled": True, "approved_by": reviewer.strip(),
                                            "approved_at": utcnow(), "note": note}, mode=0o440)


def deactivate(cfg: Config) -> None:
    if cfg.activation_path.exists():
        cfg.activation_path.chmod(0o600)
        cfg.activation_path.unlink()


def check_activation(cfg: Config) -> dict:
    if not cfg.activation_path.exists():
        raise LiveModeNotActivated("live mode has not been activated by the reviewer")
    rec = json.loads(cfg.activation_path.read_text())
    if rec.get("enabled") is not True or not rec.get("approved_by"):
        raise LiveModeNotActivated("activation record is not valid")
    return rec
