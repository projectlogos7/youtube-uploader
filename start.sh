#!/bin/bash
# Media Uploader
#   ./start.sh          start, usable from this computer only (http://localhost:8765)
#   ./start.sh --wifi   start, usable from any computer on your network
#   ./start.sh stop     stop it
#   ./start.sh logs     show what it is doing
set -e
cd "$(dirname "$0")"

export UID_GID="$(id -u):$(id -g)"

case "${1:-}" in
  stop) docker compose down; exit ;;
  logs) docker compose logs -f; exit ;;
  --wifi)
    export LAN_ACCESS=1 LISTEN_ON=0.0.0.0
    if command -v scutil >/dev/null; then
      host="$(scutil --get LocalHostName).local"
      ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
    else
      host="$(hostname -I 2>/dev/null | awk '{print $1}')"; ip="$host"
    fi
    export SERVER_URL="http://${host}:8765"
    ;;
  ""|--local)
    export LAN_ACCESS=0 LISTEN_ON=127.0.0.1 SERVER_URL=
    ;;
  *) sed -n '2,6p' "$0"; exit 1 ;;
esac

docker compose up -d --build
echo
echo "Media Uploader is running:  http://localhost:8765   (choose the watch folder there)"
if [ "$LAN_ACCESS" = 1 ]; then
  extra=""; [ -n "$ip" ] && [ "http://$ip:8765" != "$SERVER_URL" ] && extra="   (or http://$ip:8765)"
  echo "Other computers on your Wi-Fi:  $SERVER_URL$extra"
  echo "Anyone on this network can open that page, so only use --wifi on a network you trust."
fi
