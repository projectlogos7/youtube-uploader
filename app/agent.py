#!/usr/bin/env python3
"""Media Uploader helper for other computers on your network.

Watches a folder on THIS computer and sends each finished media file to the
Media Uploader, which uploads it to YouTube / Frame.io. Sent files are moved
into a "Sent" subfolder here.

  python3 media-agent.py --server http://studio-mac.local:8765 --folder ~/Movies/Upload --install
  python3 media-agent.py --uninstall

--install keeps it running in the background (macOS), also after a restart.
Without --install it runs in this Terminal window until you close it.
Standard library only; works with the Mac's built-in python3.
"""

import argparse
import hashlib
import http.client
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

MEDIA = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mxf", ".mpg", ".mpeg", ".wmv", ".flv", ".3gp",
         ".wav", ".mp3", ".aac", ".m4a", ".aif", ".aiff", ".flac",
         ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd", ".pdf"}
CONFIG = Path.home() / ".media-uploader-agent.json"
LABEL = "com.media-uploader.agent"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "media-uploader-agent.log"
SETTLE = 20      # seconds a file must stay unchanged
EVERY = 10       # seconds between checks


def say(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def connect(server):
    u = urlsplit(server)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=3600)


def post_json(server, path, payload):
    conn = connect(server)
    try:
        conn.request("POST", path, body=json.dumps(payload).encode(),
                     headers={"Content-Type": "application/json", "Host": urlsplit(server).netloc})
        r = conn.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        conn.close()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def is_open(path):
    if not shutil.which("lsof"):
        return False
    return bool(subprocess.run(["lsof", "-t", "--", str(path)], capture_output=True).stdout.strip())


def send(cfg, path):
    digest = sha256(path)
    size = path.stat().st_size
    conn = connect(cfg["server"])
    try:
        with open(path, "rb") as f:
            conn.request("POST", f"/api/incoming?name={quote(path.name)}&computer={cfg['id']}", body=f,
                         headers={"Content-Length": str(size), "Content-Type": "application/octet-stream",
                                  "X-Media-Uploader": "1", "X-Sha256": digest,
                                  "Host": urlsplit(cfg["server"]).netloc})
        r = conn.getresponse()
        body = json.loads(r.read() or b"{}")
    finally:
        conn.close()
    if r.status != 200:
        raise RuntimeError(body.get("error") or f"server answered {r.status}")


def run(cfg):
    folder = Path(cfg["folder"]).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    sent_dir = folder / "Sent"
    seen = {}
    say(f"Watching {folder} -> {cfg['server']} as \"{cfg['name']}\"")
    while True:
        files = sorted(p for p in folder.iterdir()
                       if p.is_file() and not p.name.startswith((".", "~")) and p.suffix.lower() in MEDIA)
        try:
            code, resp = post_json(cfg["server"], "/api/computers/heartbeat",
                                   {"id": cfg["id"], "name": cfg["name"], "folder": str(folder),
                                    "waiting": [p.name for p in files], "register": not cfg.get("registered")})
            if code == 200 and resp.get("removed"):
                say("This computer was removed in the web page. Stopping.")
                uninstall(quiet=True)
                sys.exit(0)
            if code == 200 and not cfg.get("registered"):
                cfg["registered"] = True
                save(cfg)
            if code != 200:
                say(f"Server answered {code}: {resp.get('error', '')}")
        except OSError as e:
            say(f"Can't reach {cfg['server']} ({e}). Retrying.")
            time.sleep(30)
            continue

        now = time.time()
        for p in files:
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            key = (st.st_size, st.st_mtime)
            if st.st_size == 0 or seen.get(p.name, (None, None, 0))[:2] != key:
                seen[p.name] = (*key, now)
                continue
            if now - seen[p.name][2] < SETTLE or now - st.st_mtime < SETTLE or is_open(p):
                continue
            say(f"Sending {p.name} ({st.st_size // (1024 * 1024)} MB)")
            try:
                send(cfg, p)
            except (OSError, RuntimeError) as e:
                say(f"  failed: {e}. Will try again.")
                continue
            sent_dir.mkdir(exist_ok=True)
            target = sent_dir / p.name
            if target.exists():
                target = sent_dir / f"{p.stem} ({int(now)}){p.suffix}"
            shutil.move(str(p), target)
            seen.pop(p.name, None)
            say(f"  sent. Moved to {target}")
        time.sleep(EVERY)


def save(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2))


def install():
    if platform.system() != "Darwin":
        say("--install is for macOS. On other systems keep this running in a terminal "
            "(or add it to your startup programs).")
        return False
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    script = Path.home() / ".media-uploader-agent.py"
    shutil.copyfile(__file__, script)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>-u</string><string>{script}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
</dict></plist>
""")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
    say(f"Installed. It runs in the background now and after restarts. Log: {LOG}")
    return True


def uninstall(quiet=False):
    if PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        PLIST.unlink()
    if not quiet:
        say("Stopped and removed from this computer.")


def main():
    ap = argparse.ArgumentParser(description="Send finished media from a folder on this computer to Media Uploader.")
    ap.add_argument("--server", help="Media Uploader address, e.g. http://studio-mac.local:8765")
    ap.add_argument("--folder", help="folder on this computer to watch")
    ap.add_argument("--name", help="name shown in the web page (default: this computer's name)")
    ap.add_argument("--install", action="store_true", help="keep running in the background (macOS)")
    ap.add_argument("--uninstall", action="store_true", help="stop and remove the background helper")
    a = ap.parse_args()

    if a.uninstall:
        uninstall()
        return
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if a.server:
        cfg["server"] = a.server.rstrip("/")
    if a.folder:
        cfg["folder"] = str(Path(a.folder).expanduser())
    if a.name or "name" not in cfg:
        host = socket.gethostname()
        cfg["name"] = a.name or (host[:-6] if host.endswith(".local") else host)
    if a.server or a.folder:
        cfg["registered"] = False
    cfg.setdefault("id", secrets.token_hex(8))
    if not cfg.get("server") or not cfg.get("folder"):
        ap.error("--server and --folder are needed the first time")
    save(cfg)
    if a.install and install():
        return
    run(cfg)


if __name__ == "__main__":
    main()
