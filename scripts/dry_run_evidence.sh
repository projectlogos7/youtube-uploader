#!/usr/bin/env bash
# Produce the "run-once dry-run evidence" for the reviewer (Done criteria).
# Runs one dry run (read-only API: channels.list only), then prints the audit
# events for that run and the request counters. Exit 0 only if zero upload
# requests were made.
set -euo pipefail
OUT="${1:-dry-run-evidence-$(date -u +%Y%m%dT%H%M%SZ).txt}"
{
  echo "== studio-uploader dry-run evidence =="
  echo "host: $(hostname)  user: $(id -un)  date: $(date -u +%FT%TZ)"
  echo "git: $(git -C "$(dirname "$0")/.." rev-parse HEAD 2>/dev/null || echo n/a)"
  echo
  studio-uploader status
  echo
  set +e
  studio-uploader run ${OFFLINE:+--offline} 2>&1
  rc=$?
  set -e
  echo
  echo "exit code: $rc"
} | tee "$OUT"
grep -q '"upload_requests": 0' "$OUT" && echo "CONFIRMED: zero upload requests" | tee -a "$OUT"
