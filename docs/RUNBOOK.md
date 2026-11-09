# Runbook: studio YouTube uploader

Audience: implementer (Arham), reviewer (studio lead), channel owner.
Every live action is Private only. Publishing is always done by hand in YouTube Studio.

---

## 0. Emergency: stop everything

Any of these works on its own. Do them in this order if unsure.

1. **Kill switch** (stops before the next API request, including retries mid-upload):
   ```bash
   sudo -u _ytuploader studio-uploader kill --reason "describe why"
   ```
   Or create the flag file directly: `touch /Users/_ytuploader/StudioUploader/KILL_SWITCH`
2. **Deactivate live mode**: `sudo -u _ytuploader studio-uploader deactivate`
3. **Revoke the OAuth grant** (section 4).

Check with `studio-uploader status`: `"kill_switch": true`, `"live_activated": false`.
To resume after review: `studio-uploader unkill` (reviewer decision).

---

## 1. One-time setup (preparation, Levels 1–2)

### 1.1 Google Cloud project (owner)
1. Owner signs in to the Google Cloud console with the channel's account, creates a project
   (record who owns the project and billing; this is still an open question in the PRD).
2. Enable **YouTube Data API v3**.
3. OAuth consent screen: External, add scopes `youtube.upload` and `youtube.readonly` only.
4. Create credentials → OAuth client ID → **Desktop app**. Note the client ID (goes in
   config) and client secret (typed once into `authorize`; stored only in the Keychain).
5. Record the actual quotas: APIs & Services → YouTube Data API v3 → Quotas. Write down the
   daily units and the Video Uploads limit in the review pack.

> **Testing-mode tokens expire after 7 days.** Either publish the app to Production and
> complete OAuth verification for the YouTube scopes, or plan a weekly re-consent
> (`authorize` again) for the pilot. The uploader stops with `auth` / `invalid_grant`
> when the token expires; it never retries silently.

### 1.2 Restricted macOS user (implementer, with an admin)
The credential is isolated by running the uploader as a dedicated non-admin user whose
login keychain no other account can open.

```bash
sudo sysadminctl -addUser _ytuploader -fullName "Studio Uploader" -password -   # prompts
sudo chmod 700 /Users/_ytuploader
```
- No admin rights, not in any shared group, no SSH key shared with other accounts.
- Install Python 3.11+ and ffmpeg (Homebrew), then as `_ytuploader`:
  `python3 -m venv ~/venv && ~/venv/bin/pip install /path/to/youtube-uploader`
- Give `_ytuploader` **read** access to the staging folder only (editors keep write access).
- Claude, Grok and the implementer's own shell run as *other* users and never as `_ytuploader`.
  Do not add `_ytuploader`'s keychain to anyone else's search list.

### 1.3 Config (implementer)
```bash
sudo -u _ytuploader -i
mkdir -p ~/.config/studio-uploader
cp /path/to/repo/config/config.example.toml ~/.config/studio-uploader/config.toml
# edit: staging_dir, oauth.client_id
```

### 1.4 Owner consent (M5, owner present)
In a GUI login session as `_ytuploader` (fast user switching), with the owner at the keyboard:
```bash
studio-uploader authorize
```
- The owner types the Desktop client secret (hidden input), the browser opens, the owner signs
  in as the channel account and grants the two scopes. Redirect goes to `127.0.0.1` on a
  random port; PKCE is used.
- The refresh token and client secret are written to `_ytuploader`'s login keychain.
- **Consent approves no video.**

### 1.5 Channel pin (owner present)
```bash
studio-uploader pin
```
Shows the channel title, custom URL and ID from `channels.list(mine=true)`. The owner checks it
is the studio channel (do not assume from the spoken name "The Network") and types the
channel ID to confirm. Stored in `~/StudioUploader/channel_pin.json`.

### 1.6 Credential isolation evidence
From the implementer's normal account (not `_ytuploader`):
```bash
scripts/isolation_check.sh _ytuploader | tee isolation-evidence.txt
```
Repeat from inside each AI agent's terminal/tool environment with `AGENT_LABEL=claude` and
`AGENT_LABEL=grok`. Every line must say `denied`; the script ends `PASS`. Attach the output to
the review pack. On the studio Mac you can also run
`STUDIO_UPLOADER_ISOLATION_TEST=1 pytest tests/test_credential_isolation.py`.

### 1.7 Preflight, dry-run evidence and review pack (M6)
```bash
sudo -u _ytuploader -i
studio-uploader doctor                       # no FAIL lines allowed
scripts/dry_run_evidence.sh                  # read-only API calls; must show "upload_requests": 0
pytest -q | tee ~/test-log.txt               # from the repo checkout
studio-uploader review-pack --isolation-evidence ~/isolation-evidence.txt --test-log ~/test-log.txt
```
The review pack folder (`~/StudioUploader/review-packs/<timestamp>/`) holds `SUMMARY.md` with the
PRD's "Done means" checklist ticked from the evidence, plus the redacted reports, audit events,
pin, approval records and ledger. The reviewer reads it, ticks "Code and config reviewed", and
signs section A of `templates/approval_record.md`.

The first live upload is the M7 pilot: follow **[PILOT.md](PILOT.md)**.

---

## 2. Per video (Level 3 only, after activation)

| Step | Who | Command |
| --- | --- | --- |
| Editor exports render, then writes `<name>.done` next to it | Editor | — |
| Check readiness | Implementer | `studio-uploader scan` |
| Create draft manifest | Implementer | `studio-uploader prepare "<path>"` |
| Fill in every field of the draft | Reviewer | edit `manifests/drafts/*.draft.json` |
| **Gate 1**: approve (freezes file + manifest, read-only) | Reviewer | `studio-uploader approve <draft> --reviewer "<name>"` |
| Dry run, check the job shows `dry_run_ok` and `private` | Reviewer | `studio-uploader run` |
| **Gate 2**: release this video for live upload | Reviewer | `studio-uploader release <approval_id> --reviewer "<name>"` |
| Bounded activation, if none is active | Reviewer | `studio-uploader activate --reviewer "<name>" --max-uploads N --expires-in-days D` |
| Upload as Private | Reviewer | `studio-uploader run --live` |
| Later: record processing result | Anyone | `studio-uploader followup` |
| Read the report: video ID, Private, processing status | Reviewer | `reports/<run_id>.json` |
| Publish (separate decision) | Reviewer | YouTube Studio, by hand |

Any edit to the frozen file or manifest after Gate 1 voids the approval; prepare and approve again.

Every activation is bounded: an upload count (default 1), an expiry (default 7 days, max 31) and,
for the pilot, a list of allowed approval IDs. `studio-uploader status` and `doctor` show what is
used. Live runs also refuse to start unless they run as `expected_user`, stop when the
`daily_upload_cap` (default 10 per Pacific day) is reached, and, in OAuth Testing mode, stop
before any call once the refresh token is 7 days old.

---

## 3. When the uploader stops

The run exits with code 2, writes `alerts/<run_id>.json`, and the reason is in the audit log
(`logs/audit-YYYYMMDD.jsonl`).

| Code | Meaning | Action |
| --- | --- | --- |
| `kill_switch` | Flag file present | Investigate; reviewer runs `unkill` |
| `not_activated` | `--live` without activation, activation expired, or wrong macOS user | Reviewer activates if intended; run as `_ytuploader` |
| `auth` | `invalid_grant`, expired/revoked consent, 401 after refresh | Owner runs `authorize` again |
| `channel_mismatch` | Zero, several or a different channel | Do not re-pin without the owner. Check which account consented |
| `quota` | Quota, rate limit, or our `daily_upload_cap` | Wait for reset (midnight Pacific); check quotas in Cloud console |
| `api_permanent` | 4xx such as invalid metadata | Fix the manifest (new approval) |
| `retries_exhausted` | Repeated 5xx or network errors | Check network; rerun resumes from the saved offset |
| `uncertain_outcome` | Final bytes may have been sent; cannot confirm a video | See 3.1 |
| `verification_failed` | Video not Private or on wrong channel | **Immediately** set it to Private/delete in Studio; engage kill switch; investigate |
| `lock_held` | Another run is active | Wait; check for a stuck process |

A job outside the activation (not in the pilot list, or over the upload count) is *refused*
(exit code 1, notification) rather than a hard stop; other jobs continue.

### 3.1 Resolving an uncertain outcome
The uploader first asks the upload session, then searches the channel's uploads by title and
upload time. If neither settles it, it stops and will not start any new upload until resolved.
1. Reviewer opens YouTube Studio → Content and looks for the video (Private) by title.
2. If found: `studio-uploader resolve <approval_id> --video-id <ID> --reviewer "<name>"`
3. If definitely not there: `studio-uploader resolve <approval_id> --not-uploaded --reviewer "<name>"`

---

## 4. Revoke and rotate

1. Kill switch on (section 0).
2. Revoke the grant:
   - From the tool: `sudo -u _ytuploader studio-uploader revoke --delete-keychain-item`
   - **And** in the owner's Google account: myaccount.google.com → Security →
     Third-party apps with account access → the app → Remove access.
3. Delete the Keychain item if not already done:
   `sudo -u _ytuploader security delete-generic-password -s studio-uploader.youtube -a channel-owner`
4. Rotate the client: Cloud console → Credentials → delete the Desktop OAuth client, create a
   new one, update `oauth.client_id`, then redo 1.4 and 1.5.
5. Note: Google keeps at most 100 refresh tokens per account per client and drops the oldest
   silently. Avoid repeated re-consent.

---

## 5. Optional scheduled runner (after the pilot)

Only once the reviewer is satisfied with the pilot. As `_ytuploader`, **logged in** (its login
keychain must be unlocked; keep the session open with fast user switching):
```bash
scripts/install_launchd.sh install ~/venv
```
- `com.studio.uploader.run`: every 15 min, `run --live --if-active`. Without an active
  activation or released work it exits with no API call. Otherwise it uploads only released
  videos within the activation, as Private.
- `com.studio.uploader.scan`: when the staging folder changes, notifies that renders are ready
  for a manifest. It prepares and uploads nothing.

Remove with `scripts/install_launchd.sh uninstall`. The kill switch and `deactivate` stop it too.

---

## 6. Files

Under `base_dir` (default `~/StudioUploader`, owned by `_ytuploader`):

| Path | Contents | Permissions |
| --- | --- | --- |
| `frozen/<approval_id>/` | Frozen video, approved manifest, approval record | read-only |
| `releases/<approval_id>.json` | Gate 2 release record | read-only |
| `activation.json` | Level 3 activation record | read-only |
| `channel_pin.json` | Confirmed channel ID | read-only |
| `state/*.state.json` | Resumable session URI and offset | 0600 in a 0700 dir |
| `ledger.jsonl` | Append-only upload ledger | 0640 |
| `logs/audit-*.jsonl` | Redacted audit log | 0640 |
| `reports/`, `alerts/` | Per-run report; hard-stop alerts | 0640 |
| `KILL_SWITCH` | Present = all API calls disabled | |
| `review-packs/<ts>/` | Evidence for sign-off (redacted) | |
