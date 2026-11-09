# studio-uploader

A small local tool for the studio Mac that uploads reviewer-approved renders to the pinned
studio YouTube channel as **Private** videos only. Built from the PRD *Studio YouTube Uploader
(gated, private-only)*.

**Status: preparation.** Live uploads are disabled until the reviewer activates them (Level 3).
`run` is a dry run unless `--live` is given *and* the reviewer has activated live mode *and*
released that specific video.

## What it will and will not do

- Uploads only files that a reviewer approved via a manifest bound to the file's SHA-256.
- Every insert is `privacyStatus=private`, `notifySubscribers=false`, never `publishAt`.
  Privacy is not a manifest field. A code-level guard rejects any other request.
- The API client can only call `channels.list`, `videos.list`, `playlistItems.list`,
  `videos.insert` (resumable) and PUTs to that upload session. There is no code path to
  `videos.update`, so it cannot change privacy or publish.
- Does not do thumbnails, playlists, captions or scheduling, and never publishes.

## Install

Python 3.11+, no runtime dependencies (standard library only). `ffprobe` and `lsof` must be on PATH.

```bash
pip install -e ".[dev]"
pytest -q
```

## Commands

| Command | Who | What |
| --- | --- | --- |
| `scan` | implementer | Readiness of files in the staging folder (never authorizes) |
| `prepare <file>` | implementer | Draft manifest with the file hash and pinned channel |
| `validate <manifest> [--draft]` | anyone | Check a manifest against the schema and YouTube limits |
| `approve <draft> --reviewer N` | reviewer | **Gate 1**: freeze file and manifest read-only, write approval record |
| `release <approval_id> --reviewer N` | reviewer | **Gate 2**: approve this video for live upload |
| `activate --reviewer N` / `deactivate` | reviewer | **Level 3** live mode on/off |
| `run [--offline]` | anyone | Dry run: every check, zero upload requests |
| `run --live` | reviewer | Upload released jobs as Private, verify, report |
| `resolve <id> --video-id X \| --not-uploaded` | reviewer | Settle an uncertain outcome |
| `kill [--reason]` / `unkill` | anyone / reviewer | Kill switch |
| `status` | anyone | Kill switch, activation, pin, in-flight uploads, jobs |
| `authorize` | owner | OAuth consent (PKCE, loopback) → Keychain |
| `pin` | owner | Confirm and pin the channel |
| `revoke [--delete-keychain-item]` | owner/reviewer | Revoke the grant |

Setup, approvals, stops, revocation: **[docs/RUNBOOK.md](docs/RUNBOOK.md)**.
Approval sheet: [templates/approval_record.md](templates/approval_record.md).
Manifest schema: [schemas/manifest.schema.json](schemas/manifest.schema.json).

## Layout

```
src/studio_uploader/
  cli.py          commands
  uploader.py     run orchestration: checks, reconcile, upload, verify, report
  youtube.py      API client: allowlist, privacy guard, kill switch, retries, hard stops
  auth.py         PKCE loopback consent, refresh, revoke
  keychain.py     macOS Keychain storage (secrets via stdin, never argv)
  approval.py     draft, approve/freeze, release, activation
  manifest.py     manifest validation (FR-5)
  staging.py      readiness checks (FR-4)
  pinning.py      channel pin (FR-3)
  ledger.py state.py lock.py killswitch.py audit.py redact.py fsutil.py
tests/            all against a mocked YouTube API (tests/fake_youtube.py)
scripts/          isolation_check.sh, dry_run_evidence.sh
```

## PRD traceability

| PRD item | Code | Tests |
| --- | --- | --- |
| FR-1 Owner authorization | `auth.py` | `test_client_auth.py` (PKCE, scopes, loopback, state, invalid_grant) |
| FR-2 Credential isolation | `keychain.py`, runbook §1.2 | `scripts/isolation_check.sh`, `test_credential_isolation.py` (studio Mac only) |
| FR-3 Channel pinning | `pinning.py`, `cli.py pin` | `test_channel_mismatch_hard_stop[*]`, `test_no_pin_is_hard_stop` |
| FR-4 Staging queue | `staging.py` | `test_staging.py` (growing, open, temp names, truncated, marker, real ffprobe/lsof) |
| FR-5 Approval manifest | `manifest.py`, `approval.py` | `test_manifest.py`, changed manifest/file tests |
| FR-6 Upload, Private only | `youtube.py` | privacy guard tests, `test_dry_run_sends_zero_upload_requests`, endpoint allowlist |
| FR-7 Post-upload verification | `uploader._verify` | `test_live_upload_happy_path` |
| RS-1 Resumable, protected state | `state.py`, `uploader._upload` | `test_crash_and_resume_from_saved_offset`, `test_state_file_is_owner_only` |
| RS-2 Per-job lock | `lock.py` | `test_second_run_on_same_job_exits`, `test_concurrent_run_exits` |
| RS-3 Ledger | `ledger.py` | happy path ledger assertions |
| RS-4 Duplicate guard | `Ledger.video_for_hash` | `test_duplicate_hash_is_skipped`, `test_second_run_does_not_reupload` |
| RS-5 Reconcile first | `uploader._reconcile_all` | three `test_uncertain_outcome_*` tests |
| RS-6 Bounded retries | `YouTubeClient.request` | `test_5xx_*`, `test_transient_network_error_mid_upload_recovers` |
| RS-7 Hard stops | `errors.py` | auth, quota, 4xx, channel tests |
| RS-8 Kill switch | `killswitch.py`, checked in `YouTubeClient.request` | `test_kill_switch_*` |
| RS-9 Redacted audit log | `redact.py`, `audit.py` | `test_logs_reports_ledger_contain_no_secrets`, redaction unit tests |
| RS-10 Runbook | `docs/RUNBOOK.md` | — |
| Secret scanning | `.github/workflows/ci.yml` (gitleaks), `.pre-commit-config.yaml` | `test_repo_contains_no_secrets` |

## Trust model notes

- Approval and release records are local files. The code checks they exist and match the
  manifest digest and file hash, but it cannot prove *who* wrote them. Keep `frozen/`,
  `releases/` and `activation.json` writable only by the restricted user, and have the
  reviewer run those commands themselves.
- The reconcile channel search matches by exact title, upload time and file size. If that is
  ambiguous it stops for the reviewer rather than guessing.
