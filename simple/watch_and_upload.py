#!/usr/bin/env python3
"""Watch a folder and upload every video in it to YouTube as PRIVATE.

  python3 watch_and_upload.py auth           # once: sign in to the channel's Google account
  python3 watch_and_upload.py frameio-auth   # once (optional): sign in to Frame.io, pick project
  python3 watch_and_upload.py                # run forever (or let launchd keep it running)

How it works, every 30 seconds:
  1. Look at every video file in WATCH_FOLDER.
  2. Wait until the file has stopped changing (export finished).
  3. Upload it to YouTube as Private (subscribers are not notified). Title = file name.
  4. Upload it to Frame.io and make a share link (if Frame.io is set up).
  5. Move it to WATCH_FOLDER/Uploaded and write both links to uploaded.log.

Standard library only. The sign-in is kept in your macOS login Keychain.
"""

import base64, hashlib, http.client, http.server, json, os, secrets, shutil
import subprocess, sys, time, webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frameio  # noqa: E402
from urllib.parse import parse_qs, urlencode, urlsplit

# ---------------------------------------------------------------- settings ---
WATCH_FOLDER  = Path(os.environ.get("WATCH_FOLDER", "~/Movies/YouTube Upload")).expanduser()
CLIENT_ID     = os.environ.get("YT_CLIENT_ID", "PASTE-YOUR-CLIENT-ID.apps.googleusercontent.com")
CATEGORY_ID   = "22"        # 22 = People & Blogs. 27 = Education, 25 = News & Politics
MADE_FOR_KIDS = False
CHECK_EVERY   = 30          # seconds between folder checks
STABLE_FOR    = 20          # file size must not change for this many seconds
VIDEO_TYPES   = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
UPLOAD_TO_YOUTUBE = True
UPLOAD_TO_FRAMEIO = True    # only used once `frameio-auth` has been run
# -----------------------------------------------------------------------------

DONE_FOLDER = WATCH_FOLDER / "Uploaded"
LOG_FILE    = WATCH_FOLDER / "uploaded.log"
KEYCHAIN    = ["-s", "simple-youtube-uploader", "-a", "youtube"]
SCOPES      = "https://www.googleapis.com/auth/youtube.upload"
CHUNK       = 8 * 1024 * 1024


def say(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


# ------------------------------------------------------------------- http ---
def request(method, url, headers=None, body=None):
    """One HTTPS request. Returns (status, headers_dict, body_bytes)."""
    u = urlsplit(url)
    conn = http.client.HTTPSConnection(u.hostname, timeout=300)
    try:
        conn.request(method, u.path + ("?" + u.query if u.query else ""), body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conn.close()


# --------------------------------------------------------------- keychain ---
def keychain_save(data):
    line = " ".join(["add-generic-password", "-U", *KEYCHAIN, "-w", "'" + json.dumps(data) + "'"])
    subprocess.run(["security", "-i"], input=line + "\n", text=True, check=True, capture_output=True)


def keychain_load():
    r = subprocess.run(["security", "find-generic-password", *KEYCHAIN, "-w"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("Not signed in. Run:  python3 watch_and_upload.py auth")
    return json.loads(r.stdout.strip())


# ------------------------------------------------------------------- auth ---
def sign_in():
    secret = input("Paste the OAuth client secret: ").strip()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class Catch(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            got.update({k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()})
            self.send_response(200); self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab.")
        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Catch)
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": CLIENT_ID, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent"})
    print("Opening browser. Sign in with the YouTube channel's Google account.")
    webbrowser.open(url)
    while not got:
        server.handle_request()
    if got.get("state") != state or "code" not in got:
        sys.exit("Sign-in failed: " + got.get("error", "unexpected response"))
    status, _, body = request("POST", "https://oauth2.googleapis.com/token",
                           {"Content-Type": "application/x-www-form-urlencoded"},
                           urlencode({"code": got["code"], "client_id": CLIENT_ID, "client_secret": secret,
                                      "code_verifier": verifier, "grant_type": "authorization_code",
                                      "redirect_uri": redirect}).encode())
    data = json.loads(body)
    if status != 200 or "refresh_token" not in data:
        sys.exit("Sign-in failed: " + str(data.get("error", status)))
    keychain_save({"refresh_token": data["refresh_token"], "client_secret": secret})
    print("Signed in and saved to Keychain. Now run:  python3 watch_and_upload.py")


class SignInExpired(Exception):
    pass


def access_token():
    saved = keychain_load()
    status, _, body = request("POST", "https://oauth2.googleapis.com/token",
                           {"Content-Type": "application/x-www-form-urlencoded"},
                           urlencode({"client_id": CLIENT_ID, "client_secret": saved["client_secret"],
                                      "refresh_token": saved["refresh_token"],
                                      "grant_type": "refresh_token"}).encode())
    data = json.loads(body or b"{}")
    if status != 200:
        if data.get("error") == "invalid_grant":
            raise SignInExpired()
        raise RuntimeError(f"token refresh failed ({status})")
    return data["access_token"]


# ----------------------------------------------------------------- upload ---
def upload(path):
    """Upload one file as Private. Returns the YouTube video ID."""
    token = access_token()
    size = path.stat().st_size
    meta = {"snippet": {"title": path.stem[:100].replace("<", "").replace(">", ""),
                        "categoryId": CATEGORY_ID},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": MADE_FOR_KIDS}}
    assert meta["status"]["privacyStatus"] == "private"
    status, hdrs, body = request(
        "POST", "https://www.googleapis.com/upload/youtube/v3/videos?"
        + urlencode({"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "false"}),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
         "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/*"},
        json.dumps(meta).encode())
    if status != 200 or "location" not in hdrs:
        raise RuntimeError(f"could not start upload ({status}): {body[:200]!r}")
    session = hdrs["location"]

    offset, failures = 0, 0
    with open(path, "rb") as f:
        while True:
            f.seek(offset)
            data = f.read(CHUNK)
            try:
                status, hdrs, body = request("PUT", session, {
                    "Authorization": f"Bearer {token}", "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}"}, data)
            except OSError:
                status = 0
            if status in (200, 201):
                return json.loads(body)["id"]
            if status == 308:
                offset = int(hdrs["range"].split("-")[1]) + 1 if "range" in hdrs else 0
                failures = 0
                say(f"  {path.name}: {offset * 100 // size}%")
                continue
            # Network error or 5xx: ask the server how much it has, then carry on.
            failures += 1
            if failures > 5:
                raise RuntimeError(f"upload kept failing (last status {status})")
            time.sleep(2 ** failures)
            try:
                status, hdrs, body = request("PUT", session, {"Authorization": f"Bearer {token}",
                                          "Content-Length": "0", "Content-Range": f"bytes */{size}"}, b"")
            except OSError:
                continue
            if status in (200, 201):
                return json.loads(body)["id"]
            if status == 308:
                offset = int(hdrs["range"].split("-")[1]) + 1 if "range" in hdrs else 0
            elif status in (401,):
                token = access_token()
            elif 400 <= status < 500:
                raise RuntimeError(f"upload rejected ({status}): {body[:200]!r}")


# ------------------------------------------------------------------ watch ---
def is_finished(path):
    """True once the file has stopped growing and no app has it open."""
    if path.name.startswith(".") or path.suffix.lower() not in VIDEO_TYPES:
        return False
    size = path.stat().st_size
    if size == 0 or time.time() - path.stat().st_mtime < STABLE_FOR:
        return False
    if shutil.which("lsof") and subprocess.run(["lsof", "-t", "--", str(path)],
                                               capture_output=True).stdout.strip():
        return False  # still open in Premiere / Final Cut / Finder copy
    return True


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def done_links(digest):
    """Links already made for this exact file: {"YouTube": url, "Frame.io": url}."""
    found = {}
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text().splitlines():
            if digest not in line:
                continue
            for part in line.split("\t"):
                if "youtu.be/" in part:
                    found["YouTube"] = part
                elif "frame.io" in part or "f.io/" in part:
                    found["Frame.io"] = part
    return found


def wanted():
    out = []
    if UPLOAD_TO_YOUTUBE:
        out.append("YouTube")
    if UPLOAD_TO_FRAMEIO and frameio.configured() and frameio_signed_in():
        out.append("Frame.io")
    return out


def frameio_signed_in():
    try:
        frameio._load()
        return True
    except frameio.SignInExpired:
        return False


def handle(path):
    digest = file_hash(path)
    DONE_FOLDER.mkdir(exist_ok=True)
    links = done_links(digest)
    if not wanted():
        say(f"WAITING  {path.name}: no destination is signed in yet")
        return
    todo = [d for d in wanted() if d not in links]
    if not todo:
        say(f"SKIP  {path.name}: already uploaded ({', '.join(links.values())})")
    for dest in todo:
        say(f"UPLOADING  {path.name} -> {dest}")
        if dest == "YouTube":
            link = f"https://youtu.be/{upload(path)}"
        else:
            link = frameio.upload(path, say)
        links[dest] = link
        with open(LOG_FILE, "a") as log:   # written at once, so a crash never repeats an upload
            log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{path.name}\t{digest}\t{dest}\t{link}\n")
        say(f"DONE  {path.name} -> {dest}: {link}" + ("  (Private)" if dest == "YouTube" else ""))
    target = DONE_FOLDER / path.name
    if target.exists():
        target = DONE_FOLDER / f"{path.stem} {digest[:8]}{path.suffix}"
    shutil.move(str(path), target)
    (DONE_FOLDER / f"{target.stem} links.txt").write_text(
        "".join(f"{k}: {v}\n" for k, v in links.items()))


def watch():
    WATCH_FOLDER.mkdir(parents=True, exist_ok=True)
    say(f"Watching {WATCH_FOLDER}. Uploading to: {', '.join(wanted()) or 'nothing (sign in first)'}. "
        "YouTube uploads are Private. Ctrl+C to stop.")
    while True:
        for path in sorted(p for p in WATCH_FOLDER.iterdir() if p.is_file()):
            try:
                if is_finished(path):
                    handle(path)
            except SignInExpired:
                say("YOUTUBE SIGN-IN EXPIRED. Run:  python3 watch_and_upload.py auth   (retrying in 10 min)")
                time.sleep(600)
                break
            except frameio.SignInExpired:
                say("FRAME.IO SIGN-IN EXPIRED. Run:  python3 watch_and_upload.py frameio-auth   (retrying in 10 min)")
                time.sleep(600)
                break
            except Exception as e:  # keep running; the file stays and is retried next round
                say(f"ERROR  {path.name}: {e}")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    if sys.argv[1:] == ["frameio-auth"]:
        frameio.sign_in()
    elif UPLOAD_TO_YOUTUBE and CLIENT_ID.startswith("PASTE-"):
        sys.exit("Set CLIENT_ID at the top of this file (or the YT_CLIENT_ID environment variable).")
    elif sys.argv[1:] == ["auth"]:
        sign_in()
    else:
        watch()
