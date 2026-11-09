#!/usr/bin/env bash
# Credential isolation check (FR-2). Run on the studio Mac by the implementer,
# from an ordinary admin/implementer account, with the reviewer watching.
#
# It tries to read the uploader's Keychain item as several identities that must
# all be DENIED, and prints PASS only if every attempt fails. It never prints a
# secret: output of any successful read is discarded and counted as a FAIL.
#
# Usage: scripts/isolation_check.sh [restricted_user] [keychain_path]
set -u
UPLOADER_USER="${1:-_ytuploader}"
KC_PATH="${2:-/Users/${UPLOADER_USER}/Library/Keychains/login.keychain-db}"
SERVICE="studio-uploader.youtube"
ACCOUNT="channel-owner"
fail=0

attempt() {  # label, command...
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "FAIL  ${label}: item was readable"
    fail=1
  else
    echo "ok    ${label}: denied"
  fi
}

echo "Isolation check $(date -u +%FT%TZ) as $(id -un) for item ${SERVICE}/${ACCOUNT}"

# 1. Implementer's normal shell, default keychain search list.
attempt "implementer shell (default keychains)" \
  security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w

# 2. Implementer's shell pointing directly at the uploader's keychain file.
attempt "implementer shell (uploader keychain path)" \
  security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w "$KC_PATH"

# 3. Reading the keychain file bytes directly.
attempt "direct file read of uploader keychain" cat "$KC_PATH"

# 4. A generic Python script (stands in for 'other scripts').
attempt "python script via security" python3 -c "
import subprocess,sys
r=subprocess.run(['security','find-generic-password','-s','$SERVICE','-a','$ACCOUNT','-w','$KC_PATH'],capture_output=True)
sys.exit(0 if r.returncode==0 and r.stdout.strip() else 1)"

# 5. Claude / Grok agent processes. Run this script again from inside each agent's
#    terminal or tool sandbox; record each run in the evidence file.
if [ -n "${AGENT_LABEL:-}" ]; then
  attempt "agent: ${AGENT_LABEL}" \
    security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w "$KC_PATH"
fi

# 6. Home directory of the restricted user must not be listable by others.
attempt "list uploader home" ls "/Users/${UPLOADER_USER}/Library/Keychains"

if [ "$fail" -eq 0 ]; then
  echo "PASS: no tested identity could read the credential"
else
  echo "FAILED: fix the Keychain/user setup before activation (docs/RUNBOOK.md §2)"
fi
exit "$fail"
