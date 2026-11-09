"""PRD acceptance tests. All run against the mocked YouTube API."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from studio_uploader import approval
from studio_uploader.errors import PrivacyViolation
from studio_uploader.ledger import Ledger
from studio_uploader.state import StateStore
from studio_uploader.youtube import assert_private, build_insert_request

from conftest import good_manifest_fields, make_job, run
from fake_youtube import (ACCESS_TOKEN, CHANNEL_ID, CLIENT_SECRET, OTHER_CHANNEL_ID,
                          REFRESH_TOKEN, FakeYouTube, SimulatedCrash)


def _assert_all_inserts_private(fake: FakeYouTube):
    for ins in fake.inserts:
        assert ins["body"]["status"]["privacyStatus"] == "private"
        assert "publishAt" not in ins["body"]["status"]
        assert ins["params"]["notifySubscribers"] == "false"


# ---- happy path ---------------------------------------------------------------

def test_live_upload_happy_path(activated, fake):
    cfg = activated
    job = make_job(cfg)
    res, _ = run(cfg, fake)
    assert res.exit_code == 0, res
    assert fake.count("insert") == 1
    [j] = res.jobs
    assert j["outcome"] == "uploaded"
    assert j["verification"]["privacyStatus"] == "private"
    assert j["verification"]["channel_id"] == CHANNEL_ID
    _assert_all_inserts_private(fake)
    statuses = [e["status"] for e in Ledger(cfg.ledger_path).entries()]
    assert statuses == ["started", "uploaded", "verified"]
    entry = Ledger(cfg.ledger_path).video_for_hash(job.manifest["file_sha256"])
    assert entry["channel_id"] == CHANNEL_ID and entry["video_id"] == j["video_id"]
    assert StateStore(cfg.state_dir).all() == []  # session URI discarded after completion


def test_dry_run_sends_zero_upload_requests(cfg, fake):
    make_job(cfg)
    res, audit = run(cfg, fake, live=False)
    assert res.exit_code == 0
    assert res.upload_requests == 0
    assert fake.count("insert") == 0 and fake.count("put") == 0
    assert fake.count("channels") == 1  # read-only pin check (Level 2)
    assert res.jobs[0]["outcome"] == "dry_run_ok"
    events = [e["event"] for e in audit.events]
    for needed in ("check.kill_switch", "check.activation", "check.channel_pin",
                   "check.reconcile", "job.approval_valid", "job.dry_run", "run.end"):
        assert needed in events
    report = json.loads((cfg.reports_dir / f"{res.run_id}.json").read_text())
    assert report["upload_requests"] == 0


def test_offline_dry_run_makes_no_api_calls(cfg, fake):
    make_job(cfg)
    res, _ = run(cfg, fake, live=False, offline=True)
    assert res.exit_code == 0
    assert fake.calls == []


def test_live_requires_reviewer_activation(cfg, fake):
    make_job(cfg)
    res, _ = run(cfg, fake)
    assert res.hard_stop["code"] == "not_activated"
    assert fake.calls == []


def test_live_requires_per_video_release(activated, fake):
    make_job(activated, release=False)
    res, _ = run(activated, fake)
    assert res.jobs[0]["outcome"] == "refused" and res.jobs[0]["code"] == "not_released"
    assert fake.count("insert") == 0


# ---- Changed manifest ------------------------------------------------------------

def test_changed_manifest_voids_approval(activated, fake):
    job = make_job(activated)
    mpath = job.dir / "manifest.json"
    os.chmod(job.dir, 0o750)
    os.chmod(mpath, 0o640)
    m = json.loads(mpath.read_text())
    m["title"] = "Edited after approval"
    mpath.write_text(json.dumps(m))
    res, _ = run(activated, fake)
    assert res.jobs[0]["outcome"] == "refused"
    assert res.jobs[0]["code"] == "approval_void"
    assert fake.count("insert") == 0


@pytest.mark.parametrize("field,value", [("tags", ["x"]), ("madeForKids", True),
                                         ("containsSyntheticMedia", True), ("description", "new")])
def test_any_manifest_field_change_voids(activated, fake, field, value):
    job = make_job(activated)
    mpath = job.dir / "manifest.json"
    os.chmod(job.dir, 0o750)
    os.chmod(mpath, 0o640)
    m = json.loads(mpath.read_text())
    m[field] = value
    mpath.write_text(json.dumps(m))
    res, _ = run(activated, fake)
    assert res.jobs[0]["code"] == "approval_void"
    assert fake.count("insert") == 0


# ---- Changed file --------------------------------------------------------------------

def test_changed_file_hash_mismatch(activated, fake):
    job = make_job(activated)
    os.chmod(job.video_path, 0o600)
    with open(job.video_path, "r+b") as fh:
        fh.seek(1234)
        b = fh.read(1)
        fh.seek(1234)
        fh.write(bytes([(b[0] + 1) % 256]))
    res, _ = run(activated, fake)
    assert res.jobs[0]["outcome"] == "refused"
    assert res.jobs[0]["code"] == "hash_mismatch"
    assert fake.count("insert") == 0


def test_frozen_files_are_read_only(cfg):
    job = make_job(cfg)
    assert not os.access(job.video_path, os.W_OK) or os.geteuid() == 0
    assert (job.video_path.stat().st_mode & 0o222) == 0
    assert ((job.dir / "manifest.json").stat().st_mode & 0o222) == 0


# ---- Duplicate ---------------------------------------------------------------------------

def test_duplicate_hash_is_skipped(activated, fake):
    job = make_job(activated)
    Ledger(activated.ledger_path).append(approval_id="earlier-job", file_sha256=job.manifest["file_sha256"],
                                         channel_id=CHANNEL_ID, status="verified", video_id="existing123")
    res, _ = run(activated, fake)
    assert res.jobs[0]["outcome"] == "skipped_duplicate"
    assert fake.count("insert") == 0 and fake.count("put") == 0


def test_second_run_does_not_reupload(activated, fake):
    make_job(activated)
    run(activated, fake)
    res, _ = run(activated, fake)
    assert res.jobs[0]["outcome"] == "skipped_duplicate"
    assert fake.count("insert") == 1


# ---- Crash and resume -----------------------------------------------------------------------

def test_crash_and_resume_from_saved_offset(activated, fake):
    job = make_job(activated, size=900_000)  # 4 chunks of 256 KiB
    puts = {"n": 0}

    def crash_after_second(session, finished):
        puts["n"] += 1
        if puts["n"] == 2:
            raise SimulatedCrash()
    fake.after_put = crash_after_second
    with pytest.raises(SimulatedCrash):
        run(activated, fake)
    st = StateStore(activated.state_dir).load(job.approval_id)
    assert st is not None and st.status == "uploading"
    saved_offset = st.offset
    fake.after_put = None

    res, _ = run(activated, fake)
    assert res.exit_code == 0, res
    assert fake.count("insert") == 1                 # one session, one video
    assert len(fake.videos) == 1
    assert res.jobs[0]["outcome"] == "uploaded"
    session = next(iter(fake.sessions.values()))
    assert session.received == session.total
    # Resumed: the first PUT after restart was a status query, and no byte before
    # the server's offset was sent again.
    puts_after = [c for c in fake.calls if c.kind == "put"][2:]
    assert puts_after[0].headers["Content-Range"].startswith("bytes */")
    assert puts_after[1].headers["Content-Range"].startswith(f"bytes {2 * 256 * 1024}-")
    assert saved_offset <= 2 * 256 * 1024


def test_transient_network_error_mid_upload_recovers(activated, fake):
    make_job(activated, size=900_000)
    fake.status_queue["put"] = [-1]  # first chunk PUT: connection reset
    res, _ = run(activated, fake)
    assert res.exit_code == 0
    assert fake.count("insert") == 1 and len(fake.videos) == 1


# ---- Uncertain outcome -----------------------------------------------------------------------

def _crash_on_final(session, finished):
    if finished:
        raise SimulatedCrash()


def test_uncertain_outcome_reconciled_via_session_before_new_job(activated, fake):
    job1 = make_job(activated, name="A.mov", seed=1)
    fake.after_put = _crash_on_final
    with pytest.raises(SimulatedCrash):
        run(activated, fake)
    assert StateStore(activated.state_dir).load(job1.approval_id).status == "finalizing"
    fake.after_put = None
    make_job(activated, name="B.mov", seed=3)
    n_before = len(fake.calls)

    res, _ = run(activated, fake)
    assert res.exit_code == 0, res
    later = fake.calls[n_before:]
    kinds = [c.kind for c in later]
    # channel check, then the reconcile status query, and only then a new insert
    first_put = kinds.index("put")
    first_insert = kinds.index("insert")
    assert later[first_put].headers["Content-Range"].startswith("bytes */")
    assert first_put < first_insert
    assert fake.count("insert") == 2  # job1 once, job2 once
    assert len(fake.videos) == 2
    entries = list(Ledger(activated.ledger_path).entries())
    assert sum(1 for e in entries if e["approval_id"] == job1.approval_id and e["status"] == "uploaded") == 1


def test_uncertain_outcome_session_gone_matched_on_channel(activated, fake):
    job = make_job(activated)
    fake.after_put = _crash_on_final
    with pytest.raises(SimulatedCrash):
        run(activated, fake)
    fake.after_put = None
    for s in fake.sessions.values():
        s.expired = True
    res, _ = run(activated, fake)
    assert res.exit_code == 0, res
    assert fake.count("playlistItems") == 1
    assert fake.count("insert") == 1
    e = Ledger(activated.ledger_path).video_for_hash(job.manifest["file_sha256"])
    assert e["status"] == "reconciled"


def test_uncertain_outcome_unresolvable_is_hard_stop(activated, fake):
    make_job(activated, name="A.mov", seed=1)
    fake.after_put = _crash_on_final
    with pytest.raises(SimulatedCrash):
        run(activated, fake)
    fake.after_put = None
    for s in fake.sessions.values():
        s.expired = True
    fake.uploads.clear()  # nothing visible on the channel
    make_job(activated, name="B.mov", seed=3)
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "uncertain_outcome"
    assert fake.count("insert") == 1  # B was never started
    assert list(activated.alerts_dir.glob("*.json"))
    # Still stopped on the next run until the reviewer resolves it.
    res2, _ = run(activated, fake)
    assert res2.hard_stop["code"] == "uncertain_outcome"
    assert fake.count("insert") == 1


# ---- Kill switch ---------------------------------------------------------------------------

def test_kill_switch_mid_queue(activated, fake):
    make_job(activated, name="A.mov", seed=1)
    make_job(activated, name="B.mov", seed=3)

    def engage_after_first_video(session, finished):
        if finished:
            activated.killswitch_path.write_text("engaged")
    fake.after_put = engage_after_first_video
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "kill_switch"
    n = len(fake.calls)
    assert fake.count("insert") == 1
    # The finished upload's own response was already received; nothing after it.
    assert fake.calls[-1].kind == "put"
    assert n == len(fake.calls)
    report = json.loads((activated.reports_dir / f"{res.run_id}.json").read_text())
    assert report["hard_stop"]["code"] == "kill_switch"


def test_kill_switch_before_run_makes_no_calls(activated, fake):
    make_job(activated)
    activated.killswitch_path.write_text("engaged")
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "kill_switch"
    assert fake.calls == []


# ---- Channel mismatch ---------------------------------------------------------------------

@pytest.mark.parametrize("channels", [
    [FakeYouTube.channel(OTHER_CHANNEL_ID)],
    [],
    [FakeYouTube.channel(CHANNEL_ID), FakeYouTube.channel(OTHER_CHANNEL_ID)],
], ids=["other-id", "zero", "two"])
def test_channel_mismatch_hard_stop(activated, channels):
    fake = FakeYouTube(channels=channels)
    make_job(activated)
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "channel_mismatch"
    assert fake.count("insert") == 0 and fake.count("put") == 0


def test_no_pin_is_hard_stop(activated, fake):
    make_job(activated)
    os.chmod(activated.pin_path, 0o600)
    activated.pin_path.unlink()
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "channel_mismatch"
    assert fake.count("insert") == 0


# ---- Privacy guard ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [{"privacyStatus": "public"}, {"privacyStatus": "unlisted"},
                                   {"publishAt": "2026-12-01T10:00:00Z"}, {"notifySubscribers": True}])
def test_privacy_requests_rejected_at_approval(cfg, extra):
    from studio_uploader.errors import ManifestInvalid
    with pytest.raises(ManifestInvalid, match="not allowed"):
        make_job(cfg, **extra)


@pytest.mark.parametrize("extra", [{"privacyStatus": "public"}, {"publishAt": "2026-12-01T10:00:00Z"}])
def test_privacy_fields_injected_after_approval_are_refused(activated, fake, extra):
    job = make_job(activated)
    mpath = job.dir / "manifest.json"
    os.chmod(job.dir, 0o750)
    os.chmod(mpath, 0o640)
    m = json.loads(mpath.read_text())
    m.update(extra)
    mpath.write_text(json.dumps(m))
    res, _ = run(activated, fake)
    assert res.jobs[0]["outcome"] == "refused"
    assert fake.count("insert") == 0


def test_insert_request_always_private():
    params, body = build_insert_request(dict(good_manifest_fields(), file_sha256="0" * 64))
    assert body["status"]["privacyStatus"] == "private"
    assert "publishAt" not in body["status"]
    assert params["notifySubscribers"] == "false"


@pytest.mark.parametrize("mutate", [
    lambda p, b: b["status"].__setitem__("privacyStatus", "public"),
    lambda p, b: b["status"].__setitem__("privacyStatus", "unlisted"),
    lambda p, b: b["status"].pop("privacyStatus"),
    lambda p, b: b["status"].__setitem__("publishAt", "2026-12-01T00:00:00Z"),
    lambda p, b: p.__setitem__("notifySubscribers", "true"),
    lambda p, b: p.pop("notifySubscribers"),
])
def test_assert_private_rejects_tampering(mutate):
    params, body = build_insert_request(dict(good_manifest_fields(), file_sha256="0" * 64))
    mutate(params, body)
    with pytest.raises(PrivacyViolation):
        assert_private(params, body)


def test_live_uploads_carry_private_and_no_notify(activated, fake):
    make_job(activated)
    run(activated, fake)
    assert fake.inserts
    _assert_all_inserts_private(fake)


# ---- Log redaction ---------------------------------------------------------------------------

def test_logs_reports_ledger_contain_no_secrets(activated, fake):
    make_job(activated, size=900_000)
    fake.status_queue["put"] = [-1]           # exercise retry logging
    run(activated, fake)
    fake2 = FakeYouTube()
    fake2.token_error = "invalid_grant"       # exercise auth-failure logging
    run(activated, fake2)
    session_ids = list(fake.sessions)
    texts = []
    for d in (activated.log_dir, activated.reports_dir, activated.alerts_dir):
        texts += [p.read_text() for p in d.glob("*")]
    texts.append(activated.ledger_path.read_text())
    blob = "\n".join(texts)
    assert blob
    for needle in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, "ya29.", "1//", "GOCSPX-",
                   "upload_id", *session_ids):
        assert needle not in blob, needle
    assert "Authorization" not in blob  # the header name, as the PRD's grep test specifies
    assert not re.search(r"(?i)bearer\s+[A-Za-z0-9]", blob)


def test_state_file_is_owner_only(activated, fake):
    job = make_job(activated, size=900_000)
    fake.after_put = lambda s, f: (_ for _ in ()).throw(SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        run(activated, fake)
    p = activated.state_dir / f"{job.approval_id}.state.json"
    assert (p.stat().st_mode & 0o777) == 0o600
    assert (activated.state_dir.stat().st_mode & 0o777) == 0o700


# ---- Hard stops: auth / quota / 4xx / retries --------------------------------------------------

def test_invalid_grant_is_hard_stop_without_retry(activated, fake):
    make_job(activated)
    fake.token_error = "invalid_grant"
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "auth"
    assert "re-authorization" in res.hard_stop["reason"]
    assert fake.count("token") == 1
    assert fake.count("insert") == 0


def test_quota_exceeded_is_hard_stop(activated, fake):
    make_job(activated)
    fake.status_queue["insert"] = [403]
    fake.error_reason = "quotaExceeded"
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "quota"
    assert fake.count("insert") == 1


def test_permanent_4xx_is_hard_stop(activated, fake):
    make_job(activated)
    fake.status_queue["insert"] = [400]
    fake.error_reason = "invalidCategoryId"
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "api_permanent"
    assert fake.count("insert") == 1


def test_5xx_retried_with_bounded_attempts(activated, fake):
    make_job(activated)
    fake.status_queue["insert"] = [503, 500]
    res, _ = run(activated, fake)
    assert res.exit_code == 0
    assert fake.count("insert") == 3
    assert len(fake.videos) == 1


def test_5xx_exhausts_retries(activated, fake):
    make_job(activated)
    fake.status_queue["insert"] = [503] * 10
    res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "retries_exhausted"
    assert fake.count("insert") == activated.retry.max_attempts


# ---- Locks -----------------------------------------------------------------------------------

def test_second_run_on_same_job_exits(activated, fake):
    from studio_uploader.lock import FileLock
    job = make_job(activated)
    with FileLock(activated.locks_dir / f"{job.approval_id}.lock"):
        res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "lock_held"
    assert fake.count("insert") == 0


def test_concurrent_run_exits(activated, fake):
    from studio_uploader.lock import FileLock
    make_job(activated)
    with FileLock(activated.locks_dir / "run.lock"):
        res, _ = run(activated, fake)
    assert res.hard_stop["code"] == "lock_held"
    assert fake.calls == []
