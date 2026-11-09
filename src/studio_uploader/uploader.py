"""Run orchestration: one pass over approved jobs.

Order of a run (each step logged):
 1. kill switch            7. per job: lock, verify approval + re-hash,
 2. run lock                  duplicate guard, live release
 3. live activation (live)  8. dry run: log what would be sent, send nothing
 4. channel pin check       9. live: resumable upload as Private
 5. reconcile uncertain     10. post-upload verification via videos.list
    outcomes first          11. report to reviewer; alert on hard stop
 6. resume interrupted uploads before new ones
 12. follow up processing status of earlier uploads (read-only)

Live runs also enforce: the restricted macOS user, the activation's expiry,
pilot scope and upload count, the daily upload cap, and Testing-mode token age.
"""

from __future__ import annotations

import getpass
import json
import mimetypes
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from . import approval
from .auth import check_token_age
from .audit import AuditLog
from .config import Config
from .errors import (ApprovalVoid, HardStop, JobRefused, LiveModeNotActivated,
                     PermanentApiError, QuotaExceeded, RetriesExhausted,
                     TransportError, UncertainOutcome, VerificationFailed)
from .fsutil import atomic_write_json
from .killswitch import KillSwitch
from .ledger import Ledger
from .lock import FileLock
from .notify import Notifier, null_notifier
from .pinning import uploads_playlist_id, verify_pin
from .state import StateStore, UploadState
from .youtube import YouTubeClient, build_insert_request, next_offset


@dataclass
class RunResult:
    run_id: str
    mode: str
    exit_code: int = 0
    hard_stop: dict | None = None
    jobs: list[dict] = field(default_factory=list)
    upload_requests: int = 0
    api_requests: int = 0
    followups: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    idle: bool = False


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Uploader:
    def __init__(self, cfg: Config, client: YouTubeClient | None, audit: AuditLog, *,
                 live: bool, offline: bool = False, sleep: Callable[[float], None] = time.sleep,
                 notifier: Notifier = null_notifier,
                 token_age_days: Callable[[], float | None] | None = None,
                 current_user: Callable[[], str] = getpass.getuser,
                 skip_if_idle: bool = False, followup_only: bool = False):
        if live and offline:
            raise ValueError("live mode cannot be offline")
        if live and (client is None or not client.live):
            raise ValueError("live mode needs a live client")
        if not live and client is not None and client.live:
            raise ValueError("dry run must use a non-live client")
        self.cfg = cfg
        self.client = client
        self.audit = audit
        self.live = live
        self.offline = offline
        self.sleep = sleep
        self.notify = notifier
        self.token_age_days = token_age_days
        self.current_user = current_user
        self.skip_if_idle = skip_if_idle
        self.followup_only = followup_only
        self.activation: dict | None = None
        self.verified_this_run: set[str] = set()
        self.killswitch = KillSwitch(cfg.killswitch_path)
        self.ledger = Ledger(cfg.ledger_path)
        self.states = StateStore(cfg.state_dir)
        self.pinned: str | None = None
        self.uploads_playlist: str | None = None
        mode = "live" if live else ("dry-run-offline" if offline else "dry-run")
        if followup_only:
            if live or offline:
                raise ValueError("follow-up is read-only and needs the API")
            mode = "followup"
        self.result = RunResult(run_id=audit.run_id, mode=mode)

    # ---- entry point -----------------------------------------------------------

    def run(self) -> RunResult:
        self.cfg.ensure_dirs()
        self.audit.info("run.start", mode=self.result.mode)
        try:
            self.killswitch.check()
            self.audit.info("check.kill_switch", engaged=False)
            if self.live and self.cfg.expected_user and self.current_user() != self.cfg.expected_user:
                raise LiveModeNotActivated(f"live runs must run as the restricted user "
                                           f"{self.cfg.expected_user!r}, not {self.current_user()!r}")
            if self.skip_if_idle and self._idle():
                self.result.idle = True
                self.audit.info("run.idle", note="no activation or no released work; no API calls made")
                return self.result
            with FileLock(self.cfg.locks_dir / "run.lock"):
                self._run()
        except HardStop as exc:
            self._hard_stop(exc)
        finally:
            if self.client is not None:
                self.result.upload_requests = self.client.stats.upload_requests
                self.result.api_requests = self.client.stats.total
            if self.result.exit_code == 0 and any(j["outcome"] in ("refused", "failed") for j in self.result.jobs):
                self.result.exit_code = 1
                refused = [j for j in self.result.jobs if j["outcome"] == "refused"]
                if refused:
                    self.notify("Studio uploader: job refused",
                                f"{len(refused)} job(s) refused: {refused[0].get('reason', '')}")
            self.audit.info("run.end", exit_code=self.result.exit_code,
                            api_requests=self.result.api_requests,
                            upload_requests=self.result.upload_requests,
                            jobs=len(self.result.jobs))
            self._write_report()
        return self.result

    def _hard_stop(self, exc: HardStop) -> None:
        self.result.exit_code = 2
        self.result.hard_stop = {"code": exc.code, "reason": str(exc)}
        self.audit.error("run.hard_stop", code=exc.code, reason=str(exc))
        self.notify(f"Studio uploader STOPPED ({exc.code})", str(exc))
        atomic_write_json(self.cfg.alerts_dir / f"{self.audit.run_id}.json",
                          {"run_id": self.audit.run_id, "code": exc.code, "reason": str(exc),
                           "action": "Reviewer attention required. See docs/RUNBOOK.md."}, mode=0o640)

    def _write_report(self) -> None:
        rep = {"run_id": self.result.run_id, "mode": self.result.mode,
               "exit_code": self.result.exit_code, "hard_stop": self.result.hard_stop,
               "api_requests": self.result.api_requests,
               "upload_requests": self.result.upload_requests, "jobs": self.result.jobs,
               "followups": self.result.followups, "warnings": self.result.warnings,
               "idle": self.result.idle, "pinned_channel_id": self.pinned,
               "activation_id": (self.activation or {}).get("activation_id")}
        atomic_write_json(self.cfg.reports_dir / f"{self.result.run_id}.json", rep, mode=0o640)

    # ---- run body ----------------------------------------------------------------

    def _run(self) -> None:
        if self.live:
            act = approval.check_activation(self.cfg)
            self.activation = act
            used = approval.uploads_under_activation(self.ledger, act["activation_id"])
            self.audit.info("check.activation", activation_id=act["activation_id"], mode=act["mode"],
                            approved_by=act["approved_by"], expires_at=act["expires_at"],
                            uploads_used=len(used), max_uploads=act["max_uploads"],
                            allowed_approval_ids=act.get("allowed_approval_ids"))
            age = self.token_age_days() if self.token_age_days else None
            warning = check_token_age(age, self.cfg.oauth)
            self.audit.info("check.token_age", age_days=None if age is None else round(age, 2),
                            testing_mode=self.cfg.oauth.testing_mode)
            if warning:
                self.result.warnings.append(warning)
                self.audit.warn("check.token_age_warning", warning=warning)
                self.notify("Studio uploader: re-consent soon", warning)
        else:
            self.audit.info("check.activation", skipped="dry run: live mode not requested")

        self.pinned = self.cfg.pinned_channel_id()
        if self.offline:
            self.audit.info("check.channel_pin", pinned_channel_id=self.pinned,
                            skipped="offline dry run: channels.list not called")
        else:
            ch = verify_pin(self.client, self.pinned)
            self.uploads_playlist = uploads_playlist_id(ch)
            self.audit.info("check.channel_pin", ok=True, channel_id=ch["id"],
                            channel_title=ch.get("snippet", {}).get("title"))

        if self.followup_only:
            self._followups()
            return

        self._reconcile_all()

        ids = approval.list_jobs(self.cfg)
        # Resume interrupted uploads before starting anything new.
        in_flight = {s.approval_id for s in self.states.all()}
        ids.sort(key=lambda i: (i not in in_flight, i))
        self.audit.info("queue.scan", approved_jobs=len(ids), in_flight=len(in_flight))
        for aid in ids:
            self.killswitch.check()
            self._job(aid)

        if not self.offline:
            self._followups()

    def _idle(self) -> bool:
        """True when a scheduled live run has nothing to do; makes no API call."""
        if not self.cfg.activation_path.exists():
            return True
        if self.states.all():
            return False
        if self.ledger.videos_needing_followup():
            return False
        for aid in approval.list_jobs(self.cfg):
            if not (self.cfg.releases_dir / f"{aid}.json").exists():
                continue
            try:
                h = approval.load_job(self.cfg, aid).manifest.get("file_sha256", "")
            except JobRefused:
                return False  # let the real run report it
            if not self.ledger.video_for_hash(h):
                return False
        return True

    # ---- processing follow-up (FR-7) ---------------------------------------------

    def _followups(self) -> None:
        pending = self.ledger.videos_needing_followup()
        for vid, entry in pending.items():
            if vid in self.verified_this_run:
                continue
            self.killswitch.check()
            out = self._verify(entry["approval_id"], entry["file_sha256"], vid, enforce_private=False)
            self.result.followups.append({"video_id": vid, "approval_id": entry["approval_id"], **out})
    # ---- reconcile (RS-5) ---------------------------------------------------------

    def _reconcile_all(self) -> None:
        pending = self.states.all()
        if not pending:
            self.audit.info("check.reconcile", pending=0)
            return
        for st in pending:
            self.killswitch.check()
            if st.status == "complete":
                self._record_complete(st)
                continue
            if st.status not in ("finalizing", "uncertain"):
                continue  # ordinary interrupted upload; resumed in its job
            if not self.live:
                self.audit.warn("check.reconcile", approval_id=st.approval_id, status=st.status,
                                note="uncertain outcome pending; a live run reconciles it before any new upload")
                continue
            if st.status == "uncertain":
                raise UncertainOutcome(f"job {st.approval_id} has an unresolved uncertain outcome; "
                                       "reviewer must check YouTube Studio and run `resolve`")
            self._reconcile(st)

    def _reconcile(self, st: UploadState) -> None:
        self.audit.warn("reconcile.start", approval_id=st.approval_id)
        self.client.trust_session(st.session_uri)
        resp = self.client.query_status(st.session_uri, st.total_bytes)
        if resp.status in (200, 201):
            self._finalize(st, resp, how="reconciled_via_session")
            return
        if resp.status == 308:
            st.offset = next_offset(resp)
            st.status = "uploading"
            self.states.save(st)
            self.audit.info("reconcile.incomplete", approval_id=st.approval_id, offset=st.offset,
                            note="server did not receive the final bytes; will resume")
            return
        # Session gone: look for the video on the channel.
        match = self._find_on_channel(st)
        if match:
            self.ledger.append(approval_id=st.approval_id, file_sha256=st.file_sha256,
                               channel_id=st.channel_id, status="reconciled", video_id=match,
                               method="channel_search")
            self.states.delete(st.approval_id)
            self.audit.info("reconcile.matched", approval_id=st.approval_id, video_id=match)
            self._verify(st.approval_id, st.file_sha256, match)
            return
        st.status = "uncertain"
        self.states.save(st)
        self.ledger.append(approval_id=st.approval_id, file_sha256=st.file_sha256,
                           channel_id=st.channel_id, status="uncertain")
        raise UncertainOutcome(f"could not determine whether job {st.approval_id} created a video; "
                               "reviewer must check YouTube Studio and run `resolve`")

    def _find_on_channel(self, st: UploadState) -> str | None:
        if not self.uploads_playlist:
            return None
        since = _parse_ts(st.started_at) - timedelta(minutes=5)
        candidates = []
        for item in self.client.recent_uploads(self.uploads_playlist):
            sn = item.get("snippet", {})
            vid = item.get("contentDetails", {}).get("videoId") or sn.get("resourceId", {}).get("videoId")
            ts = sn.get("publishedAt") or item.get("contentDetails", {}).get("videoPublishedAt")
            if not vid or sn.get("title") != st.title or not ts or _parse_ts(ts) < since:
                continue
            candidates.append(vid)
        if len(candidates) != 1:
            self.audit.warn("reconcile.search", approval_id=st.approval_id, candidates=len(candidates))
            return None
        vid = candidates[0]
        v = self.client.video(vid)
        size = (v or {}).get("fileDetails", {}).get("fileSize")
        if size is not None and int(size) != st.total_bytes:
            self.audit.warn("reconcile.size_mismatch", approval_id=st.approval_id, video_id=vid)
            return None
        return vid

    # ---- per job ------------------------------------------------------------------

    def _job(self, aid: str) -> None:
        rec = {"approval_id": aid, "outcome": None}
        self.result.jobs.append(rec)
        try:
            with FileLock(self.cfg.locks_dir / f"{aid}.lock"):
                self._job_locked(aid, rec)
        except JobRefused as exc:
            rec.update(outcome="refused", code=exc.code, reason=str(exc))
            self.audit.warn("job.refused", approval_id=aid, code=exc.code, reason=str(exc))
        except HardStop as exc:
            rec.update(outcome="failed", code=exc.code, reason=str(exc))
            raise

    def _job_locked(self, aid: str, rec: dict) -> None:
        job = approval.load_job(self.cfg, aid)
        file_hash = job.manifest.get("file_sha256", "")

        prior = self.ledger.video_for_hash(file_hash) if file_hash else None
        if prior:
            rec.update(outcome="skipped_duplicate", video_id=prior["video_id"])
            self.audit.info("job.duplicate", approval_id=aid, video_id=prior["video_id"],
                            note="hash already uploaded; no insert")
            return

        approval.verify_job(job, self.pinned or "")  # re-hash immediately before upload
        self.audit.info("job.approval_valid", approval_id=aid, file_sha256=file_hash,
                        approved_by=job.manifest["approved_by"])

        for other in self.states.all():
            if other.file_sha256 == file_hash and other.approval_id != aid:
                raise ApprovalVoid(f"same file is in flight under approval {other.approval_id}")

        params, body = build_insert_request(job.manifest)

        if not self.live:
            released = (self.cfg.releases_dir / f"{aid}.json").exists()
            rec.update(outcome="dry_run_ok", title=job.manifest["title"], released=released,
                       privacyStatus=body["status"]["privacyStatus"],
                       notifySubscribers=params["notifySubscribers"])
            self.audit.info("job.dry_run", approval_id=aid, would_send={"params": params, "body": body},
                            released_for_live=released, note="no upload request sent")
            return

        rel = approval.check_released(self.cfg, job)
        self.audit.info("job.released", approval_id=aid, released_by=rel["released_by"])
        approval.check_activation_scope(self.activation, job, self.ledger)
        video_id = self._upload(job)
        rec.update(outcome="uploaded", video_id=video_id)
        rec["verification"] = self._verify(aid, file_hash, video_id)
        self.notify("Studio uploader: uploaded as Private",
                    f"{job.manifest['title'][:60]} -> video {video_id}. Publication stays with the reviewer.")

    # ---- upload (FR-6, RS-1, RS-6) --------------------------------------------------

    def _upload(self, job: approval.Job) -> str:
        total = job.video_path.stat().st_size
        ctype = mimetypes.guess_type(job.video_path.name)[0] or "application/octet-stream"
        st = self.states.load(job.approval_id)
        if st and st.file_sha256 != job.manifest["file_sha256"]:
            raise ApprovalVoid("saved upload state belongs to a different file")

        if st and st.session_uri:
            self.client.trust_session(st.session_uri)
            resp = self.client.query_status(st.session_uri, total)
            if resp.status in (200, 201):
                return self._finalize(st, resp, how="resume_found_complete")
            if resp.status == 308:
                st.offset = next_offset(resp)
                st.status = "uploading"
                self.states.save(st)
                self.audit.info("upload.resume", approval_id=job.approval_id, offset=st.offset, total=total)
            else:
                # Session expired before the final chunk: no video exists. Start over.
                self.audit.warn("upload.session_expired", approval_id=job.approval_id)
                self.states.delete(job.approval_id)
                st = None

        if not st or not st.session_uri:
            today = self.ledger.starts_today()
            if today >= self.cfg.daily_upload_cap:
                raise QuotaExceeded(f"daily upload cap reached ({today}/{self.cfg.daily_upload_cap} "
                                    "since midnight Pacific)")
            self.ledger.append(approval_id=job.approval_id, file_sha256=job.manifest["file_sha256"],
                               channel_id=self.pinned, status="started",
                               activation_id=(self.activation or {}).get("activation_id"))
            uri = self.client.init_resumable(job.manifest, total, ctype)
            st = UploadState(approval_id=job.approval_id, file_sha256=job.manifest["file_sha256"],
                             channel_id=self.pinned or "", title=job.manifest["title"],
                             total_bytes=total, session_uri=uri, status="initiated")
            self.states.save(st)
            self.audit.info("upload.session_created", approval_id=job.approval_id, total=total)

        failures = 0
        with open(job.video_path, "rb") as fh:
            while True:
                if st.offset >= total:
                    # All bytes acknowledged but no resource yet: ask for it.
                    resp = self.client.query_status(st.session_uri, total)
                    if resp.status in (200, 201):
                        return self._finalize(st, resp, how="status_query")
                    raise PermanentApiError(f"server holds all bytes but returned HTTP {resp.status}")
                fh.seek(st.offset)
                data = fh.read(self.cfg.chunk_size)
                final = st.offset + len(data) >= total
                st.status = "finalizing" if final else "uploading"
                self.states.save(st)  # persisted BEFORE sending: a crash now is detectable
                try:
                    resp = self.client.put_chunk(st.session_uri, data, st.offset, total)
                except TransportError as exc:
                    failures += 1
                    if failures >= self.cfg.retry.max_attempts:
                        raise RetriesExhausted(f"upload interrupted {failures} times: {exc}") from None
                    delay = self.client._backoff(failures - 1)
                    self.audit.warn("upload.retry", approval_id=job.approval_id, attempt=failures,
                                    reason=str(exc), delay_s=round(delay, 2))
                    self.sleep(delay)
                    resp = self.client.query_status(st.session_uri, total)
                    if resp.status in (404, 410):
                        if final:
                            raise UncertainOutcome("session vanished after the final chunk was sent")
                        raise PermanentApiError("upload session expired mid-upload")
                if resp.status in (200, 201):
                    return self._finalize(st, resp, how="final_chunk")
                new_offset = next_offset(resp)
                if new_offset > st.offset:
                    failures = 0
                st.offset = new_offset
                st.status = "uploading"
                self.states.save(st)
                self.audit.info("upload.progress", approval_id=job.approval_id, offset=st.offset, total=total)

    def _finalize(self, st: UploadState, resp, *, how: str) -> str:
        try:
            video = json.loads(resp.body or b"{}")
            video_id = video["id"]
        except (ValueError, KeyError, TypeError):
            raise UncertainOutcome("upload completed but the response carried no video ID") from None
        st.status = "complete"
        st.video_id = video_id
        self.states.save(st)
        self._record_complete(st, how=how)
        return video_id

    def _record_complete(self, st: UploadState, how: str = "recovered_state") -> None:
        last = self.ledger.latest_for(st.approval_id)
        if not (last and last.get("video_id") == st.video_id):
            self.ledger.append(approval_id=st.approval_id, file_sha256=st.file_sha256,
                               channel_id=st.channel_id, status="uploaded", video_id=st.video_id, how=how)
        self.states.delete(st.approval_id)  # session URI no longer needed
        self.audit.info("upload.complete", approval_id=st.approval_id, video_id=st.video_id, how=how)

    # ---- post-upload verification (FR-7) ----------------------------------------------

    def _verify(self, aid: str, file_hash: str, video_id: str, *, enforce_private: bool = True) -> dict:
        """Check a video on the channel. ``enforce_private`` is off for later
        follow-ups, because the reviewer may since have published it by hand."""
        self.verified_this_run.add(video_id)
        v = self.client.video(video_id)
        if v is None:
            self.audit.warn("verify.not_found_yet", approval_id=aid, video_id=video_id)
            return {"found": False}
        channel = v.get("snippet", {}).get("channelId")
        status = v.get("status", {})
        pd = v.get("processingDetails", {})
        out = {"found": True, "channel_id": channel, "privacyStatus": status.get("privacyStatus"),
               "uploadStatus": status.get("uploadStatus"),
               "failureReason": status.get("failureReason"),
               "rejectionReason": status.get("rejectionReason"),
               "processingStatus": pd.get("processingStatus"),
               "processingFailureReason": pd.get("processingFailureReason")}
        self.ledger.append(approval_id=aid, file_sha256=file_hash, channel_id=channel,
                           status="verified", video_id=video_id, verification=out)
        self.audit.info("verify.result", approval_id=aid, video_id=video_id, **out)
        if channel != self.pinned:
            raise VerificationFailed(f"video {video_id} is on an unexpected channel")
        if enforce_private and status.get("privacyStatus") != "private":
            raise VerificationFailed(f"video {video_id} is not private; set it to Private in Studio now")
        return out
