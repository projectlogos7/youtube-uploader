#!/usr/bin/env bash
# Install (or remove) the optional LaunchAgents for the restricted uploader user.
# Run AS the restricted user, inside its logged-in GUI session (fast user
# switching): the login keychain holding the credential is only unlocked while
# that user is logged in.
#
#   scripts/install_launchd.sh install  [venv_dir] [staging_dir]
#   scripts/install_launchd.sh uninstall
set -euo pipefail
ACTION="${1:-}"
VENV="${2:-$HOME/venv}"
STAGING="${3:-}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABELS=(com.studio.uploader.run com.studio.uploader.scan)

case "$ACTION" in
  install)
    [ -x "$VENV/bin/studio-uploader" ] || { echo "studio-uploader not found in $VENV"; exit 1; }
    if [ -z "$STAGING" ]; then
      STAGING="$(python3 -c "import tomllib,os;print(os.path.expanduser(tomllib.load(open(os.path.expanduser('~/.config/studio-uploader/config.toml'),'rb'))['staging_dir']))")"
    fi
    "$VENV/bin/studio-uploader" doctor || { echo "doctor reported failures; fix them first"; exit 1; }
    mkdir -p "$AGENTS"
    for L in "${LABELS[@]}"; do
      sed -e "s#__VENV__#$VENV#g" -e "s#__HOME__#$HOME#g" -e "s#__STAGING__#$STAGING#g" \
        "$HERE/templates/launchd/$L.plist" > "$AGENTS/$L.plist"
      plutil -lint "$AGENTS/$L.plist"
      launchctl bootout "$DOMAIN/$L" 2>/dev/null || true
      launchctl bootstrap "$DOMAIN" "$AGENTS/$L.plist"
      echo "loaded $L"
    done
    echo "Scheduled runs only upload reviewer-released videos inside an active, bounded activation."
    ;;
  uninstall)
    for L in "${LABELS[@]}"; do
      launchctl bootout "$DOMAIN/$L" 2>/dev/null || true
      rm -f "$AGENTS/$L.plist"
      echo "removed $L"
    done
    ;;
  *) echo "usage: $0 install [venv] [staging] | uninstall"; exit 2 ;;
esac
