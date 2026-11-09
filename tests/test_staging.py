"""FR-4 readiness checks, including the 'Incomplete file' acceptance test."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from studio_uploader.staging import (candidates, check_ready, evaluate_probe,
                                     ffprobe_prober, lsof_open_checker)

GOOD_PROBE = {"format": {"duration": "12.5"},
              "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}


def closed(_):
    return False


def good(_):
    return GOOD_PROBE


def _file(cfg, name="Episode.mov", data=b"x" * 5000, marker=True):
    p = cfg.staging_dir / name
    p.write_bytes(data)
    if marker:
        Path(str(p) + cfg.readiness.marker_suffix).write_text("")
    return p


def test_ready_file(cfg):
    r = check_ready(_file(cfg), cfg, sleep=lambda s: None, open_checker=closed, prober=good)
    assert r.ready, r.reasons


def test_growing_file_not_ready(cfg):
    p = _file(cfg)

    def grow(_):
        with open(p, "ab") as fh:
            fh.write(b"more")
    r = check_ready(p, cfg, sleep=grow, open_checker=closed, prober=good)
    assert not r.ready
    assert "still being written" in r.reasons[0]


def test_open_by_writer_not_ready(cfg):
    r = check_ready(_file(cfg), cfg, sleep=lambda s: None, open_checker=lambda _: True, prober=good)
    assert not r.ready and "open by another process" in r.reasons[0]


def test_unknown_open_state_fails_closed(cfg):
    r = check_ready(_file(cfg), cfg, sleep=lambda s: None, open_checker=lambda _: None, prober=good)
    assert not r.ready and "failing closed" in r.reasons[0]


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not installed")
def test_open_by_writer_real_lsof(cfg):
    p = _file(cfg)
    with open(p, "ab"):
        r = check_ready(p, cfg, sleep=lambda s: None, open_checker=lsof_open_checker(), prober=good)
    assert not r.ready and "open by another process" in r.reasons[0]
    r2 = check_ready(p, cfg, sleep=lambda s: None, open_checker=lsof_open_checker(), prober=good)
    assert r2.ready, r2.reasons


@pytest.mark.parametrize("name", ["Episode.mov.part", "Episode.mov.tmp", ".Episode.mov",
                                  "~Episode.mov", "Episode.mov.crdownload", "Episode partial.mov",
                                  "Episode_rendering.mov", "Episode.mov.download"])
def test_temp_names_not_ready(cfg, name):
    r = check_ready(_file(cfg, name), cfg, sleep=lambda s: None, open_checker=closed, prober=good)
    assert not r.ready
    assert any("temporary" in x or "extension" in x for x in r.reasons)


def test_empty_file_not_ready(cfg):
    r = check_ready(_file(cfg, data=b""), cfg, sleep=lambda s: None, open_checker=closed, prober=good)
    assert not r.ready and "file is empty" in r.reasons


def test_missing_marker_not_ready(cfg):
    r = check_ready(_file(cfg, marker=False), cfg, sleep=lambda s: None, open_checker=closed, prober=good)
    assert not r.ready and "marker" in r.reasons[0]


def test_recently_modified_not_ready(cfg):
    cfg.readiness.min_age_s = 60
    r = check_ready(_file(cfg), cfg, sleep=lambda s: None, open_checker=closed, prober=good)
    assert not r.ready and "modified less than" in r.reasons[0]


def test_outside_staging_not_ready(cfg, tmp_path):
    p = tmp_path / "elsewhere.mov"
    p.write_bytes(b"x" * 100)
    r = check_ready(p, cfg, sleep=lambda s: None, open_checker=closed, prober=good)
    assert not r.ready and "outside" in r.reasons[0]


def test_symlink_not_accepted(cfg, tmp_path):
    target = _file(cfg, "Real.mov")
    link = cfg.staging_dir / "Link.mov"
    link.symlink_to(target)
    Path(str(link) + ".done").write_text("")
    r = check_ready(link, cfg, sleep=lambda s: None, open_checker=closed, prober=good)
    assert not r.ready


@pytest.mark.parametrize("probe,reason", [
    ({"format": {}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}, "duration"),
    ({"format": {"duration": "3"}, "streams": [{"codec_type": "audio"}]}, "no video"),
    ({"format": {"duration": "3"}, "streams": [{"codec_type": "video"}]}, "no audio"),
])
def test_probe_requirements(probe, reason):
    assert any(reason in r for r in evaluate_probe(probe))


def test_truncated_container_not_ready(cfg):
    def broken(_):
        raise RuntimeError("moov atom not found")
    r = check_ready(_file(cfg), cfg, sleep=lambda s: None, open_checker=closed, prober=broken)
    assert not r.ready and "ffprobe could not read" in r.reasons[0]


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
def test_real_ffprobe_valid_and_truncated(cfg):
    p = cfg.staging_dir / "Real.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=1", "-shortest", "-pix_fmt", "yuv420p", str(p)],
                   check=True)
    Path(str(p) + ".done").write_text("")
    r = check_ready(p, cfg, sleep=lambda s: None, open_checker=closed, prober=ffprobe_prober())
    assert r.ready, r.reasons
    t = cfg.staging_dir / "Trunc.mp4"
    t.write_bytes(p.read_bytes()[: p.stat().st_size // 3])
    Path(str(t) + ".done").write_text("")
    r = check_ready(t, cfg, sleep=lambda s: None, open_checker=closed, prober=ffprobe_prober())
    assert not r.ready


def test_candidates_only_top_level_staging(cfg):
    _file(cfg, "A.mov")
    (cfg.staging_dir / "sub").mkdir()
    (cfg.staging_dir / "sub" / "B.mov").write_bytes(b"x")
    names = [p.name for p in candidates(cfg)]
    assert names == ["A.mov"]


def test_readiness_does_not_create_jobs(cfg):
    check_ready(_file(cfg), cfg, sleep=lambda s: None, open_checker=closed, prober=good)
    assert list(cfg.frozen_dir.iterdir()) == []
