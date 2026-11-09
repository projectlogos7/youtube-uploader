"""Configuration loading.

The config file holds no secrets. The refresh token and OAuth client secret
live only in the macOS Keychain (see keychain.py). The confirmed channel pin is
written by ``studio-uploader pin`` into ``<base_dir>/channel_pin.json``.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReadinessConfig:
    stable_checks: int = 3          # size+mtime must match across this many checks
    stable_interval_s: float = 5.0  # seconds between checks
    require_marker: bool = True     # require an export-complete sidecar
    marker_suffix: str = ".done"    # e.g. "Episode 7.mov.done"
    min_age_s: float = 30.0         # mtime must be at least this old
    video_extensions: tuple[str, ...] = (".mp4", ".mov", ".m4v", ".mkv", ".webm")
    ffprobe_path: str = "ffprobe"
    lsof_path: str = "lsof"


@dataclass
class RetryConfig:
    max_attempts: int = 5
    base_delay_s: float = 2.0
    max_delay_s: float = 64.0


@dataclass
class KeychainConfig:
    service: str = "studio-uploader.youtube"
    account: str = "channel-owner"
    # Path to the restricted uploader user's keychain. Empty = default keychain
    # of the user running the process (which must be the restricted user).
    keychain_path: str = ""


@dataclass
class OAuthConfig:
    client_id: str = ""             # Desktop OAuth client ID (not secret)
    auth_uri: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"
    revoke_uri: str = "https://oauth2.googleapis.com/revoke"


@dataclass
class Config:
    base_dir: Path
    staging_dir: Path
    chunk_size: int = 8 * 1024 * 1024
    readiness: ReadinessConfig = field(default_factory=ReadinessConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    keychain: KeychainConfig = field(default_factory=KeychainConfig)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)
    expected_account_hint: str = ""  # shown during consent; informational only

    # ---- derived paths (all under base_dir) -------------------------------
    @property
    def drafts_dir(self) -> Path:
        return self.base_dir / "manifests" / "drafts"

    @property
    def frozen_dir(self) -> Path:
        return self.base_dir / "frozen"

    @property
    def releases_dir(self) -> Path:
        return self.base_dir / "releases"

    @property
    def state_dir(self) -> Path:
        return self.base_dir / "state"

    @property
    def locks_dir(self) -> Path:
        return self.base_dir / "locks"

    @property
    def log_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.base_dir / "reports"

    @property
    def alerts_dir(self) -> Path:
        return self.base_dir / "alerts"

    @property
    def ledger_path(self) -> Path:
        return self.base_dir / "ledger.jsonl"

    @property
    def killswitch_path(self) -> Path:
        return self.base_dir / "KILL_SWITCH"

    @property
    def activation_path(self) -> Path:
        return self.base_dir / "activation.json"

    @property
    def pin_path(self) -> Path:
        return self.base_dir / "channel_pin.json"

    def pinned_channel_id(self) -> str | None:
        if not self.pin_path.exists():
            return None
        data = json.loads(self.pin_path.read_text())
        cid = data.get("channel_id")
        return cid if isinstance(cid, str) and cid else None

    def ensure_dirs(self) -> None:
        for d in (self.drafts_dir, self.frozen_dir, self.releases_dir,
                  self.locks_dir, self.log_dir, self.reports_dir, self.alerts_dir):
            d.mkdir(parents=True, exist_ok=True)
        # State holds resumable session URIs: owner-only (RS-1).
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)


def _build(cls, data: dict | None):
    data = dict(data or {})
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    if "video_extensions" in data:
        data["video_extensions"] = tuple(e.lower() for e in data["video_extensions"])
    return cls(**data)


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    base = Path(os.path.expanduser(raw.pop("base_dir"))).resolve()
    staging = Path(os.path.expanduser(raw.pop("staging_dir"))).resolve()
    cfg = Config(
        base_dir=base,
        staging_dir=staging,
        chunk_size=int(raw.pop("chunk_size", 8 * 1024 * 1024)),
        readiness=_build(ReadinessConfig, raw.pop("readiness", None)),
        retry=_build(RetryConfig, raw.pop("retry", None)),
        keychain=_build(KeychainConfig, raw.pop("keychain", None)),
        oauth=_build(OAuthConfig, raw.pop("oauth", None)),
        expected_account_hint=raw.pop("expected_account_hint", ""),
    )
    if raw:
        raise ValueError(f"unknown config keys: {sorted(raw)}")
    if cfg.chunk_size % (256 * 1024) != 0:
        raise ValueError("chunk_size must be a multiple of 256 KiB (resumable protocol)")
    if staging == base or base in staging.parents:
        raise ValueError("staging_dir must not be inside base_dir")
    return cfg
