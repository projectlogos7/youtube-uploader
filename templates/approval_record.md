# Approval record

One record per gate. Keep the signed copy with the review pack. The uploader
also writes machine records (`frozen/<id>/approval.json`, `releases/<id>.json`,
`activation.json`); this sheet is the human record that sits alongside them.

## A. Activation (Level 3). Once, before the pilot

| Item | Value |
| --- | --- |
| Reviewer | |
| Date (UTC) | |
| Code version (git commit) | |
| Dry-run evidence file reviewed | |
| Pinned channel title / ID (as confirmed by owner) | |
| OAuth mode: Production-verified **or** Testing with weekly re-consent | |
| Kill switch tested (`studio-uploader kill`, then `status`) | yes / no |
| Review pack folder and `doctor` result (no FAIL) | |
| Activation ID (printed by `activate`) | |
| Scope: pilot approval ID(s) **or** "any released video" | |
| Max uploads / expiry | |
| Command run | `studio-uploader activate --reviewer "<name>" --pilot <approval_id> --max-uploads 1 --expires-in-days 1` |
| Signature | |

## B. Per video: manifest approval (gate 1)

| Item | Value |
| --- | --- |
| approval_id (printed by `approve`) | |
| Source file name | |
| file_sha256 (first 12 chars) | |
| Title | |
| Audience: madeForKids | true / false |
| containsSyntheticMedia | true / false |
| License and rights note checked | yes |
| Watched the full render | yes |
| Not *Sharjeel Final v2* | confirmed |
| Reviewer / date (UTC) | |

## C. Per video: live release (gate 2)

| Item | Value |
| --- | --- |
| approval_id | |
| `studio-uploader run` dry-run shows this job `dry_run_ok`, `privacyStatus: private` | yes |
| Command run | `studio-uploader release <approval_id> --reviewer "<name>"` |
| Reviewer / date (UTC) | |

## D. After upload

| Item | Value |
| --- | --- |
| Video ID | |
| Verified on pinned channel, Private (from run report) | yes |
| uploadStatus / processingStatus | |
| Rejection or failure reason (if any) | |
| Published manually in YouTube Studio? (reviewer only, separate decision) | no / yes, date |
