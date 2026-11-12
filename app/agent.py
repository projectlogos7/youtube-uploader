#!/usr/bin/env python3
"""Media Uploader helper for other computers on your Wi-Fi.

Install once (the web app shows this exact line with the right address):

  curl -fsSL http://MAIN:8765/agent.py -o ~/media-agent.py && python3 ~/media-agent.py --server http://MAIN:8765 --install

After that everything is done in the web app: choose this computer's watch folder there.
The helper watches that folder and sends each finished media file to the main computer,
which does the uploading. Sent files are moved into a "Sent" folder here.

  python3 ~/media-agent.py --uninstall      stop and remove it

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
import threading
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
SETTLE = 20      # seconds a file must stay unchanged before it's sent
EVERY = 5        # seconds between folder checks

state = {"folder": "", "waiting": [], "problem": ""}


def say(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


# ---- talking to the main computer ------------------------------------------------

def connect(server, timeout=60):
    u = urlsplit(server)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout)


def post_json(server, path, payload, timeout=60):
    conn = connect(server, timeout)
    try:
        conn.request("POST", path, body=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
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


def send(cfg, path):
    digest = sha256(path)
    conn = connect(cfg["server"], timeout=3600)
    try:
        with open(path, "rb") as f:
            conn.request("POST", f"/api/incoming?name={quote(path.name)}&computer={cfg['id']}", body=f,
                         headers={"Content-Length": str(path.stat().st_size),
                                  "Content-Type": "application/octet-stream",
                                  "X-Media-Uploader": "1", "X-Sha256": digest})
        r = conn.getresponse()
        body = json.loads(r.read() or b"{}")
    finally:
        conn.close()
    if r.status != 200:
        raise RuntimeError(body.get("error") or f"main computer answered {r.status}")


# ---- folder picker support -----------------------------------------------------

def list_dirs(path):
    home = Path.home()
    try:
        p = Path(path).expanduser().resolve() if path else home
    except (OSError, RuntimeError):
        p = home
    if not p.is_dir():
        p = home
    try:
        dirs = sorted((c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")), key=str.lower)
    except OSError:
        dirs = []
    shortcuts = [str(home)]
    if Path("/Volumes").is_dir():
        shortcuts += [str(v) for v in sorted(Path("/Volumes").iterdir()) if v.is_dir()]
    return {"path": str(p), "parent": None if p == p.parent else str(p.parent), "dirs": dirs,
            "shortcuts": shortcuts}


# ---- the two loops ----------------------------------------------------------------

def poll_loop(cfg):
    """Check in with the main computer; it answers with this computer's folder and any requests."""
    first = True
    while True:
        try:
            code, resp = post_json(cfg["server"], "/api/computers/poll",
                                   {"id": cfg["id"], "name": cfg["name"], "waiting": state["waiting"],
                                    "problem": state["problem"], "register": not cfg.get("registered"),
                                    "no_wait": first}, timeout=60)
        except OSError as e:
            say(f"Can't reach the main computer at {cfg['server']} ({e}). Retrying.")
            time.sleep(15)
            continue
        if code != 200:
            say(f"Main computer answered {code}: {resp.get('error', '')}")
            time.sleep(15)
            continue
        first = False
        if resp.get("removed"):
            say("This computer was removed in the web app. Stopping.")
            uninstall(quiet=True)
            quit_helper()
            return
        if not cfg.get("registered"):
            cfg["registered"] = True
            save(cfg)
            say("Connected. Choose this computer's watch folder in the web app.")
        if resp.get("folder") != state["folder"]:
            state["folder"] = resp.get("folder") or ""
            say(f"Watch folder: {state['folder'] or '(none chosen yet)'}")
        for job in resp.get("jobs", []):
            if job.get("type") == "browse":
                try:
                    post_json(cfg["server"], "/api/computers/reply",
                              {"job": job["job"], "result": list_dirs(job.get("path", ""))})
                except OSError:
                    pass


def quit_helper():
    os._exit(0)   # exit code 0 tells macOS not to restart it


def watch_loop(cfg):
    seen = {}
    while True:
        time.sleep(EVERY)
        folder = Path(state["folder"]) if state["folder"] else None
        if not folder:
            state["waiting"] = []
            continue
        if not folder.is_dir():
            state["problem"] = f"Folder not found: {folder}"
            continue
        state["problem"] = ""
        files = sorted(p for p in folder.iterdir()
                       if p.is_file() and not p.name.startswith((".", "~")) and p.suffix.lower() in MEDIA)
        state["waiting"] = [p.name for p in files]
        now = time.time()
        for p in files:
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            key = (st.st_size, st.st_mtime)
            if st.st_size == 0 or seen.get(str(p), (None, None, 0))[:2] != key:
                seen[str(p)] = (*key, now)
                continue
            if now - seen[str(p)][2] < SETTLE or now - st.st_mtime < SETTLE or is_open(p):
                continue
            say(f"Sending {p.name} ({st.st_size // (1024 * 1024)} MB)")
            try:
                send(cfg, p)
            except (OSError, RuntimeError) as e:
                state["problem"] = f"Couldn't send {p.name}: {e}"
                say(f"  failed: {e}. Will try again.")
                continue
            sent = folder / "Sent"
            sent.mkdir(exist_ok=True)
            target = sent / p.name
            if target.exists():
                target = sent / f"{p.stem} ({int(now)}){p.suffix}"
            shutil.move(str(p), target)
            seen.pop(str(p), None)
            say(f"  sent; the main computer uploads it now. Moved to {target}")


def is_open(path):
    if not shutil.which("lsof"):
        return False
    return bool(subprocess.run(["lsof", "-t", "--", str(path)], capture_output=True).stdout.strip())


# ---- setup -------------------------------------------------------------------------

def save(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2))


def install():
    if platform.system() != "Darwin":
        say("Background install is for Macs. Here, keep this window open (or add it to startup programs).")
        return False
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    script = Path.home() / ".media-uploader-agent.py"
    if Path(__file__).resolve() != script:
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
    say("Installed and running in the background (also after restarts).")
    say("Now open the web app and choose this computer's watch folder.")
    return True


def uninstall(quiet=False):
    if PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        PLIST.unlink()
    if not quiet:
        say("Stopped and removed from this computer.")


def main():
    ap = argparse.ArgumentParser(description="Send media from a folder on this computer to Media Uploader.")
    ap.add_argument("--server", help="main computer's address, e.g. http://studio-mac.local:8765")
    ap.add_argument("--name", help="name shown in the web app (default: this computer's name)")
    ap.add_argument("--install", action="store_true", help="keep running in the background (Mac)")
    ap.add_argument("--uninstall", action="store_true", help="stop and remove")
    a = ap.parse_args()
    if a.uninstall:
        uninstall()
        return
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if a.server and a.server.rstrip("/") != cfg.get("server"):
        cfg["server"], cfg["registered"] = a.server.rstrip("/"), False
    if a.name or "name" not in cfg:
        host = socket.gethostname()
        cfg["name"] = a.name or (host[:-6] if host.endswith(".local") else host)
    cfg.setdefault("id", secrets.token_hex(8))
    if not cfg.get("server"):
        ap.error("--server is needed the first time")
    save(cfg)
    if a.install and install():
        return
    say(f"Connecting to {cfg['server']} as \"{cfg['name']}\"")
    threading.Thread(target=watch_loop, args=(cfg,), daemon=True).start()
    poll_loop(cfg)


if __name__ == "__main__":
    main()
