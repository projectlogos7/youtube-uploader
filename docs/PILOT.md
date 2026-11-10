# M7 pilot procedure (Level 3)

The pilot is one Private upload of one reviewer-chosen test file, done by the
**reviewer** on the studio Mac. No AI agent runs any step below. The test file is **not** *Sharjeel Final v2*.

Estimated time: 30–45 minutes, plus YouTube processing time.

## Entry criteria (all required)

- [ ] Review pack signed off (M6): `studio-uploader review-pack --test-log …`,
      every box in `SUMMARY.md` ticked, and "Code and config reviewed" ticked by the reviewer.
- [ ] `studio-uploader doctor` shows no `FAIL`.
- [ ] OAuth decision recorded: Production-verified, **or** Testing mode with the refresh token
      less than 5 days old (re-run `authorize` with the owner first if needed).
- [ ] Actual quota limits from the Cloud console recorded.
- [ ] Pilot test file chosen by the reviewer and exported with its `.done` marker.

## Steps

All commands in Terminal on the studio Mac, with the venv active.

1. **Kill switch drill.** `studio-uploader kill --reason "pilot drill"` → `studio-uploader status`
   shows `"kill_switch": true` → `studio-uploader run` stops with `kill_switch` and 0 API calls →
   `studio-uploader unkill`.
2. **Readiness.** `studio-uploader scan` lists the pilot file as `ready: true`.
3. **Draft.** `studio-uploader prepare "<staging>/<pilot file>"`. Reviewer fills every field of the
   draft (title marked as a test, audience, synthetic-media flag, rights note).
4. **Gate 1.** `studio-uploader approve <draft> --reviewer "<name>"`. Note the `approval_id`.
5. **Dry run.** `studio-uploader run`. The job shows `dry_run_ok`, `privacyStatus: private`,
   `notifySubscribers: false`; `upload_requests: 0`.
6. **Gate 2.** `studio-uploader release <approval_id> --reviewer "<name>"`.
7. **Pilot activation**, scoped to this one video, one upload, short expiry:
   ```bash
   studio-uploader activate --reviewer "<name>" --pilot <approval_id> --max-uploads 1 --expires-in-days 1 --note "M7 pilot"
   ```
8. **Upload.** `studio-uploader run --live`. Expect `outcome: uploaded`, a video ID, and
   `verification.privacyStatus: private` on the pinned channel. Any other job is refused as out of
   pilot scope.
9. **Deactivate.** `studio-uploader deactivate`.
10. **Check in YouTube Studio** (reviewer): the video is Private, on the right channel, with the
    approved title and audience setting, and subscribers were not notified.
11. **Processing follow-up.** After processing finishes: `studio-uploader followup` records
    `uploadStatus: processed` / `processingStatus: succeeded` (or the rejection reason).
12. **Duplicate check.** `studio-uploader run` (dry run) shows the pilot job as
    `skipped_duplicate`.
13. **Record.** `studio-uploader review-pack` again, fill in sections C and D of
    `templates/approval_record.md`, sign.

## Exit criteria

- Exactly one video created, Private, on the pinned channel; ledger shows
  `started → uploaded → verified` for the pilot `approval_id`.
- No `FAIL` in `doctor`; no alerts other than the kill-switch drill.
- Audit log and review pack contain no tokens, `Authorization` headers or session URIs
  (`grep -rE 'ya29\.|1//|Authorization|upload_id' ~/StudioUploader` returns nothing).

## Abort

At any point: `studio-uploader kill`, then follow docs/RUNBOOK.md §0. If a video was created
with anything other than Private, set it to Private in YouTube Studio first.

## After the pilot

Publishing the pilot video, if ever, is a separate manual reviewer decision in YouTube Studio.
Regular use then repeats steps 2–9 per video. A standard activation
(`--max-uploads N --expires-in-days D`, no `--pilot`) can cover several released videos. The
optional scheduled runner (`scripts/install_launchd.sh`) only uploads released videos inside
an active activation.
