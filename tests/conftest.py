from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest

from studio_uploader import approval
from studio_uploader.audit import AuditLog
from studio_uploader.auth import RefreshTokenProvider
from studio_uploader.config import Config, OAuthConfig, ReadinessConfig, RetryConfig
from studio_uploader.keychain import MemoryStore
from studio_uploader.killswitch import KillSwitch
from studio_uploader.pinning import save_pin
from studio_uploader.staging import Readiness
from studio_uploader.uploader import Uploader
from studio_uploader.youtube import YouTubeClient

from fake_youtube import CHANNEL_ID, CLIENT_SECRET, REFRESH_TOKEN, FakeYouTube

CHUNK = 256 * 1024


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config(base_dir=tmp_path / "base", staging_dir=tmp_path / "staging", chunk_size=CHUNK,
               readiness=ReadinessConfig(stable_checks=3, stable_interval_s=0, min_age_s=0),
               retry=RetryConfig(max_attempts=4, base_delay_s=0.01, max_delay_s=0.05),
               oauth=OAuthConfig(client_id="fake-client.apps.googleusercontent.com"))
    c.staging_dir.mkdir(parents=True)
    c.ensure_dirs()
    save_pin(c, FakeYouTube.channel(CHANNEL_ID), confirmed_by="owner")
    return c


@pytest.fixture
def fake() -> FakeYouTube:
    return FakeYouTube()


def good_manifest_fields(**over) -> dict:
    d = {"title": "Pilot test video", "description": "Internal pilot. Not for publication.",
         "tags": ["pilot", "studio test"], "categoryId": "27", "defaultLanguage": "en",
         "madeForKids": False, "license": "youtube",
         "rights_note": "All footage shot in-house; music licensed (Epidemic Sound).",
         "containsSyntheticMedia": False}
    d.update(over)
    return d


def stage_file(cfg: Config, name: str = "Pilot.mov", size: int = 700_000, seed: int = 1) -> Path:
    p = cfg.staging_dir / name
    p.write_bytes(bytes((i * seed + i // 7) % 251 for i in range(size)))
    Path(str(p) + cfg.readiness.marker_suffix).write_text("")
    return p


def make_job(cfg: Config, name: str = "Pilot.mov", size: int = 700_000, seed: int = 1,
             release: bool = True, **fields) -> approval.Job:
    import json
    p = stage_file(cfg, name, size, seed)
    draft_path = approval.create_draft(cfg, Readiness(p, True, []))
    d = json.loads(draft_path.read_text())
    d.update(good_manifest_fields(**fields))
    draft_path.write_text(json.dumps(d))
    job = approval.approve(cfg, draft_path, "Studio Lead")
    if release:
        approval.release(cfg, job.approval_id, "Studio Lead")
    return job


def client_for(cfg: Config, fake: FakeYouTube, audit: AuditLog, *, live: bool) -> YouTubeClient:
    store = MemoryStore({"refresh_token": REFRESH_TOKEN, "client_id": cfg.oauth.client_id,
                         "client_secret": CLIENT_SECRET})
    tokens = RefreshTokenProvider(cfg.oauth, store, fake)
    return YouTubeClient(fake, tokens, KillSwitch(cfg.killswitch_path), audit,
                         live=live, retry=cfg.retry, sleep=lambda s: None)


def run(cfg: Config, fake: FakeYouTube, *, live: bool = True, offline: bool = False):
    audit = AuditLog(cfg.log_dir, echo=io.StringIO())
    client = None if offline else client_for(cfg, fake, audit, live=live)
    up = Uploader(cfg, client, audit, live=live, offline=offline, sleep=lambda s: None)
    return up.run(), audit


@pytest.fixture
def activated(cfg: Config) -> Config:
    approval.activate(cfg, "Studio Lead", "pilot")
    return cfg
