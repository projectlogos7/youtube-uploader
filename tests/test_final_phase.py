"""Level 3 / pilot controls: activation scope and expiry, daily cap, restricted
user, token age, idle scheduled runs, processing follow-up, notifications."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from studio_uploader import approval
from studio_uploader.auth import check_token_age
from studio_uploader.config import OAuthConfig
from studio_uploader.errors import AuthError, LiveModeNotActivated
from studio_uploader.ledger import Ledger

from conftest import make_job, run
from fake_youtube import CHANNEL_ID, FakeYouTube


def test_pilot_activation_allows_only_named_job(cfg, fake):
    pilot = make_job(cfg, name="Pilot.mov", seed=1)
    other = make_job(cfg, name="Other.mov", seed=3)
    act = approval.activate(cfg, "Studio Lead", "M7 pilot", pilot_ids=[pilot.approval_id])
    assert act["mode"] == "pilot" and act["max_uploads"] == 1
    res, _ = run(cfg, fake)
    by_id = {j["approval_id"]: j for j in res.jobs}
    assert by_id[pilot.approval_id]["outcome"] == "uploaded"
    assert by_id[other.approval_id]["outcome"] == "refused"
    assert "pilot" in by_id[other.approval_id]["reason"]
    assert fake.count("insert") == 1


def test_activation_upload_count_is_enforced(cfg, fake):
    make_job(cfg, name="A.mov", seed=1)
    make_job(cfg, name="B.mov", seed=3)
    approval.activate(cfg, "Studio Lead", max_uploads=1)
    res, _ = run(cfg, fake)
    outcomes = sorted(j["outcome"] for j in res.jobs)
    assert outcomes == ["refused", "uploaded"]
    assert fake.count("insert") == 1
    # A fresh activation is needed for the next one.
    approval.deactivate(cfg)
    approval.activate(cfg, "Studio Lead", max_uploads=1)
    res, _ = run(cfg, fake)
    assert fake.count("insert") == 2


def test_resumed_job_is_not_double_counted(cfg, fake):
    from fake_youtube import SimulatedCrash
    make_job(cfg, size=900_000)
    approval.activate(cfg, "Studio Lead", max_uploads=1)
    fake.after_put = lambda s, f: (_ for _ in ()).throw(SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        run(cfg, fake)
    fake.after_put = None
    res, _ = run(cfg, fake)
    assert res.jobs[0]["outcome"] == "uploaded"


def test_expired_activation_is_hard_stop(cfg, fake):
    make_job(cfg)
    past = datetime.now(timezone.utc) - timedelta(days=8)
    approval.activate(cfg, "Studio Lead", max_uploads=1, expires_in_days=7, now=past)
    res, _ = run(cfg, fake)
    assert res.hard_stop["code"] == "not_activated" and "expired" in res.hard_stop["reason"]
    assert fake.calls == []


def test_activation_without_limits_is_rejected(cfg, fake):
    make_job(cfg)
    cfg.activation_path.write_text(json.dumps({"enabled": True, "approved_by": "x", "activation_id": "a"}))
    res, _ = run(cfg, fake)
    assert res.hard_stop["code"] == "not_activated"


def test_activate_rejects_bad_parameters(cfg):
    with pytest.raises(LiveModeNotActivated):
        approval.activate(cfg, "Studio Lead", max_uploads=0)
    with pytest.raises(LiveModeNotActivated):
        approval.activate(cfg, "Studio Lead", expires_in_days=90)


def test_pilot_activation_requires_valid_job(cfg):
    from studio_uploader.errors import ApprovalVoid
    with pytest.raises(ApprovalVoid):
        approval.activate(cfg, "Studio Lead", pilot_ids=["20260101-000000000000"])


def test_daily_upload_cap(activated, fake):
    activated.daily_upload_cap = 1
    led = Ledger(activated.ledger_path)
    led.append(approval_id="earlier", file_sha256="f" * 64, channel_id=CHANNEL_ID, status="started")
    make_job(activated)
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "quota" and "daily upload cap" in res.hard_stop["reason"]
    assert fake.count("insert") == 0


def test_starts_today_boundary(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    p = tmp_path / "l.jsonl"
    entries = [{"ts": "2026-11-09T07:59:00+00:00", "status": "started"},   # 23:59 PST Nov 8
               {"ts": "2026-11-09T08:01:00+00:00", "status": "started"}]   # 00:01 PST Nov 9
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    assert led.starts_today(datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)) == 1


def test_live_refuses_wrong_macos_user(activated, fake):
    activated.expected_user = "_ytuploader"
    make_job(activated)
    res, _ = run(activated, fake, current_user=lambda: "arham")
    assert res.hard_stop["code"] == "not_activated" and "_ytuploader" in res.hard_stop["reason"]
    assert fake.calls == []
    res, _ = run(activated, fake, current_user=lambda: "_ytuploader")
    assert res.exit_code == 0


def test_token_age_rules():
    oauth = OAuthConfig(testing_mode=True, token_max_age_days=7)
    assert check_token_age(2.0, oauth) is None
    assert "under a day" in check_token_age(6.5, oauth)
    with pytest.raises(AuthError):
        check_token_age(7.1, oauth)
    assert check_token_age(30, OAuthConfig(testing_mode=False)) is None
    assert check_token_age(None, oauth) is None


def test_expired_testing_token_stops_before_any_call(activated, fake):
    make_job(activated)
    res, _ = run(activated, fake, token_age_days=lambda: 7.5)
    assert res.hard_stop["code"] == "auth"
    assert fake.calls == []


def test_token_warning_is_reported(activated, fake):
    make_job(activated)
    seen = []
    res, _ = run(activated, fake, token_age_days=lambda: 6.4, notifier=lambda t, m: seen.append(t))
    assert res.exit_code == 0 and res.warnings
    assert any("re-consent" in t for t in seen)


def test_idle_scheduled_run_makes_no_calls(cfg, fake):
    make_job(cfg)
    res, _ = run(cfg, fake, skip_if_idle=True)        # not activated
    assert res.idle and res.exit_code == 0 and fake.calls == []
    approval.activate(cfg, "Studio Lead", max_uploads=5)
    res, _ = run(cfg, fake, skip_if_idle=True)        # released job pending -> works
    assert not res.idle and fake.count("insert") == 1
    fake.videos[res.jobs[0]["video_id"]]["status"]["uploadStatus"] = "processed"
    fake.videos[res.jobs[0]["video_id"]]["processingDetails"]["processingStatus"] = "succeeded"
    res, _ = run(cfg, fake, skip_if_idle=True)        # follow-up pending -> runs
    assert not res.idle and res.followups[0]["processingStatus"] == "succeeded"
    n = len(fake.calls)
    res, _ = run(cfg, fake, skip_if_idle=True)        # nothing left
    assert res.idle and len(fake.calls) == n


def test_followup_records_processing_result(activated, fake):
    make_job(activated)
    res, _ = run(activated, fake)
    vid = res.jobs[0]["video_id"]
    assert res.followups == []  # just verified in this run
    v = fake.videos[vid]
    v["status"].update(uploadStatus="rejected", rejectionReason="copyright")
    res, _ = run(activated, fake, live=False)    # read-only follow-up also in dry run
    assert res.followups[0]["rejectionReason"] == "copyright"
    assert res.upload_requests == 0
    assert vid not in Ledger(activated.ledger_path).videos_needing_followup()


def test_followup_tolerates_manual_publication(activated, fake):
    make_job(activated)
    res, _ = run(activated, fake)
    vid = res.jobs[0]["video_id"]
    fake.videos[vid]["status"]["privacyStatus"] = "public"   # reviewer published in Studio
    res, _ = run(activated, fake)
    assert res.exit_code == 0 and res.followups[0]["privacyStatus"] == "public"


def test_notifications_on_stop_and_upload(activated, fake):
    seen = []
    make_job(activated)
    run(activated, fake, notifier=lambda t, m: seen.append((t, m)))
    assert any("uploaded as Private" in t for t, _ in seen)
    activated.killswitch_path.write_text("x")
    run(activated, fake, notifier=lambda t, m: seen.append((t, m)))
    assert any("STOPPED (kill_switch)" in t for t, _ in seen)


def test_ledger_tags_activation(activated, fake):
    make_job(activated)
    run(activated, fake)
    act = json.loads(activated.activation_path.read_text())
    started = [e for e in Ledger(activated.ledger_path).entries() if e["status"] == "started"]
    assert started[0]["activation_id"] == act["activation_id"]
