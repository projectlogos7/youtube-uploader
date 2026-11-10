from __future__ import annotations

import json
import time
from pathlib import Path

from studio_uploader import approval, doctor, reviewpack
from studio_uploader.cli import build_parser
from studio_uploader.keychain import MemoryStore

from conftest import make_job, run
from fake_youtube import ACCESS_TOKEN, CLIENT_SECRET, REFRESH_TOKEN


def _store(age_days=1.0):
    return MemoryStore({"refresh_token": REFRESH_TOKEN, "client_id": "c", "client_secret": CLIENT_SECRET,
                        "obtained_at": int(time.time() - age_days * 86400)})


def _doc(cfg, **kw):
    kw.setdefault("store", _store())
    kw.setdefault("which", lambda name: f"/usr/bin/{name}")
    kw.setdefault("system", "Darwin")
    return {c.name: c for c in doctor.run_doctor(cfg, **kw)}


def test_doctor_healthy(cfg):
    cfg.oauth.client_id = "x.apps.googleusercontent.com"
    checks = _doc(cfg)
    fails = [c for c in checks.values() if c.status == "fail"]
    assert fails == [], fails
    assert checks["credential in Keychain"].status == "ok"
    assert checks["channel pinned by owner"].status == "ok"


def test_doctor_flags_problems(cfg):
    cfg.oauth.client_id = ""
    checks = _doc(cfg, store=MemoryStore(), which=lambda n: None)
    for name in ("ffprobe available", "lsof available",
                 "credential in Keychain", "OAuth Desktop client ID configured"):
        assert checks[name].status == "fail", name
    assert doctor.summarize(list(checks.values())) == 1


def test_doctor_expired_testing_token(cfg):
    checks = _doc(cfg, store=_store(age_days=8))
    assert checks["refresh token age"].status == "fail"


def test_doctor_never_prints_secrets(cfg):
    out = doctor.format_checks(doctor.run_doctor(cfg, store=_store(), which=lambda n: "/x", system="Darwin"))
    for s in (REFRESH_TOKEN, CLIENT_SECRET, ACCESS_TOKEN):
        assert s not in out


def test_review_pack(cfg, fake, tmp_path):
    job = make_job(cfg)
    run(cfg, fake, live=False)
    approval.activate(cfg, "Studio Lead", pilot_ids=[job.approval_id])
    run(cfg, fake)
    tl = tmp_path / "tests.txt"
    tl.write_text("154 passed, 1 skipped in 6.49s\n")
    d = reviewpack.build(cfg, doctor.run_doctor(cfg, store=_store()), test_log=tl)
    summary = (d / "SUMMARY.md").read_text()
    for line in ("[x] Dry-run evidence", "[x] Channel pinned",
                 "[x] All acceptance tests passing", "[ ] Code and config reviewed"):
        assert line in summary, line
    assert "Latest live run" in summary and "private" in summary
    assert json.loads((d / "dry-run-report.json").read_text())["upload_requests"] == 0
    assert (d / "approvals" / f"{job.approval_id}.approval.json").exists()
    blob = "\n".join(p.read_text() for p in d.rglob("*") if p.is_file())
    for s in (REFRESH_TOKEN, CLIENT_SECRET, ACCESS_TOKEN, "upload_id"):
        assert s not in blob


def test_review_pack_unticked_without_evidence(cfg):
    d = reviewpack.build(cfg, doctor.run_doctor(cfg, store=None, system="Linux"))
    s = (d / "SUMMARY.md").read_text()
    assert "[ ] Dry-run evidence" in s and "[ ] All acceptance tests passing" in s


def test_offline_dry_run_does_not_count_as_evidence(cfg, fake):
    make_job(cfg)
    run(cfg, fake, live=False, offline=True)
    d = reviewpack.build(cfg, [])
    assert "[ ] Dry-run evidence" in (d / "SUMMARY.md").read_text()


def test_failing_test_log_not_ticked(cfg, tmp_path):
    tl = tmp_path / "t.txt"
    tl.write_text("2 failed, 150 passed in 5s\n")
    d = reviewpack.build(cfg, [], test_log=tl)
    assert "[ ] All acceptance tests passing" in (d / "SUMMARY.md").read_text()


def test_followup_only_mode(activated, fake):
    make_job(activated)
    first, _ = run(activated, fake)
    vid = first.jobs[0]["video_id"]
    make_job(activated, name="Second.mov", seed=5)
    res, _ = run(activated, fake, live=False, followup_only=True)
    assert res.mode == "followup" and res.jobs == []
    assert [f["video_id"] for f in res.followups] == [vid]
    assert res.upload_requests == 0


def test_cli_parser_new_commands():
    p = build_parser()
    a = p.parse_args(["activate", "--reviewer", "L", "--pilot", "id1", "--max-uploads", "1"])
    assert a.pilot == ["id1"] and a.expires_in_days == 7.0
    assert p.parse_args(["run", "--live", "--if-active"]).if_active
    for cmd in (["followup"], ["doctor", "--json"], ["review-pack", "--test-log", "x"], ["scan", "--notify"]):
        p.parse_args(cmd)


def test_launchd_templates_are_valid_plists():
    import plistlib
    root = Path(__file__).resolve().parents[1] / "templates" / "launchd"
    for p in root.glob("*.plist"):
        text = p.read_text().replace("__VENV__", "/v").replace("__HOME__", "/h").replace("__STAGING__", "/s")
        d = plistlib.loads(text.encode())
        args = d["ProgramArguments"]
        assert args[0] == "/v/bin/studio-uploader"
        if "run" in args:
            assert "--if-active" in args and "--live" in args
        assert "publish" not in " ".join(args)
