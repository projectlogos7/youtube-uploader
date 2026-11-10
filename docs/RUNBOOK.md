# Runbook: studio YouTube uploader

Audience: implementer (Arham), reviewer (studio lead), channel owner.
All commands run in your normal Mac account (activate the venv first: `source ~/youtube-uploader/.venv/bin/activate`).
Every live action is Private only. Publishing is always done by hand in YouTube Studio.

---

## 0. Emergency: stop everything

```bash
studio-uploader kill --reason "describe why"     # stops before the next API request
studio-uploader deactivate                       # turns live mode off
```
Check with `studio-uploader status`. To resume later: `studio-uploader unkill`.
To cut access completely, see section 4 (revoke).

---

## 1. One-time setup

All commands run in your normal Mac account, in Terminal.

### 1.1 Install
```bash
brew install python@3.12 ffmpeg
git clone https://github.com/projectlogos7/youtube-uploader.git ~/youtube-uploader
cd ~/youtube-uploader
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

### 1.2 Google Cloud (channel owner's account)
1. Go to console.cloud.google.com signed in as the channel owner. Create a project.
2. **APIs & Services → Library**: enable **YouTube Data API v3**.
3. **OAuth consent screen**: External. Add scopes `youtube.upload` and `youtube.readonly`.
   Add the channel owner's email as a test user.
4. **Credentials → Create credentials → OAuth client ID → Desktop app.**
   Copy the **client ID** and **client secret**.

> In Testing mode Google expires the sign-in after **7 days**; just run `authorize` again
> when the tool says so. Publishing the app to Production removes that limit.

### 1.3 Config
```bash
mkdir -p ~/.config/studio-uploader
cp config/config.example.toml ~/.config/studio-uploader/config.toml
open -e ~/.config/studio-uploader/config.toml
```
Set `staging_dir` (the folder editors export finished renders to) and `oauth.client_id`.

### 1.4 Sign in (owner present)
```bash
studio-uploader authorize
```
Paste the client secret when asked (hidden). The browser opens; sign in as the channel account
and allow access. The sign-in is saved in your login Keychain. **Signing in approves no video.**

### 1.5 Pin the channel (owner present)
```bash
studio-uploader pin
```
Check the title and ID shown are the studio channel, then type the channel ID to confirm.

### 1.6 Check everything
```bash
studio-uploader doctor            # no FAIL lines
studio-uploader run               # dry run: must show "upload_requests": 0
pytest -q | tee ~/test-log.txt
studio-uploader review-pack --test-log ~/test-log.txt
```
The reviewer reads `~/StudioUploader/review-packs/<time>/SUMMARY.md` and signs off.
The first real upload is the pilot: follow **[PILOT.md](PILOT.md)**.

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
used. Live runs also stop when the
`daily_upload_cap` (default 10 per Pacific day) is reached, and, in OAuth Testing mode, stop
before any call once the refresh token is 7 days old.

---

## 3. When the uploader stops

The run exits with code 2, writes `alerts/<run_id>.json`, and the reason is in the audit log
(`logs/audit-YYYYMMDD.jsonl`).

| Code | Meaning | Action |
| --- | --- | --- |
| `kill_switch` | Flag file present | Investigate; reviewer runs `unkill` |
| `not_activated` | `--live` without activation, or activation expired | Reviewer activates if intended |
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
   - From the tool: `studio-uploader revoke --delete-keychain-item`
   - **And** in the owner's Google account: myaccount.google.com → Security →
     Third-party apps with account access → the app → Remove access.
3. Delete the Keychain item if not already done:
   `security delete-generic-password -s studio-uploader.youtube -a channel-owner`
4. Rotate the client: Cloud console → Credentials → delete the Desktop OAuth client, create a
   new one, update `oauth.client_id`, then redo 1.4 and 1.5.
5. Note: Google keeps at most 100 refresh tokens per account per client and drops the oldest
   silently. Avoid repeated re-consent.

---

## 5. Optional scheduled runner (after the pilot)

Only once the reviewer is satisfied with the pilot. Your Mac user must be logged in (the
Keychain must be unlocked):
```bash
scripts/install_launchd.sh install ~/youtube-uploader/.venv
```
- `com.studio.uploader.run`: every 15 min, `run --live --if-active`. Without an active
  activation or released work it exits with no API call. Otherwise it uploads only released
  videos within the activation, as Private.
- `com.studio.uploader.scan`: when the staging folder changes, notifies that renders are ready
  for a manifest. It prepares and uploads nothing.

Remove with `scripts/install_launchd.sh uninstall`. The kill switch and `deactivate` stop it too.

---

## 6. Files

Under `base_dir` (default `~/StudioUploader`):

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
