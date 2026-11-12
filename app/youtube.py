"""YouTube connector: Google sign-in (PKCE, loopback redirect) and resumable upload.

Videos are always uploaded as Private, without notifying subscribers.
"""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

from . import net


def _sleep(seconds):  # replaced in tests
    time.sleep(seconds)

SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.readonly")
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHUNK = 8 * 1024 * 1024
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mxf", ".mpg", ".mpeg", ".wmv", ".flv", ".3gp"}

_pending = {}  # state -> sign-in in progress


class NeedsReconnect(Exception):
    pass


def accepts(path):
    return path.suffix.lower() in VIDEO_TYPES


def connected(store):
    return bool(store.secret("youtube") and store.secret("youtube").get("refresh_token"))


def start_auth(store, client_id, client_secret, redirect_uri):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _pending.clear()
    _pending[state] = {"client_id": client_id, "client_secret": client_secret,
                       "verifier": verifier, "redirect_uri": redirect_uri}
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent"})


def finish_from_address(store, pasted_address):
    """Finish sign-in from the address the browser ended on (used from other computers,
    where Google's redirect to localhost can't reach this app)."""
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(pasted_address.strip()).query).items()}
    if q.get("error"):
        raise ValueError(f"Google sign-in was not completed: {q['error']}")
    if "code" not in q:
        raise ValueError("That address has no sign-in code. Click Connect and try again.")
    return finish_auth(store, q["code"], q.get("state", ""))


def finish_auth(store, code, state):
    p = _pending.pop(state, None)
    if not p:
        raise ValueError("This sign-in link has expired. Click Connect again.")
    status, _, body = net.request("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                  urlencode({"code": code, "client_id": p["client_id"],
                                             "client_secret": p["client_secret"], "code_verifier": p["verifier"],
                                             "grant_type": "authorization_code",
                                             "redirect_uri": p["redirect_uri"]}).encode())
    data = json.loads(body or b"{}")
    if status != 200 or "refresh_token" not in data:
        raise ValueError(f"Google sign-in failed: {data.get('error_description') or data.get('error') or status}")
    store.set_secret("youtube", {"client_secret": p["client_secret"], "refresh_token": data["refresh_token"]})
    channel = _channel_name(data["access_token"])
    store.update({"youtube": {"client_id": p["client_id"], "channel": channel}})
    return channel


def disconnect(store):
    store.set_secret("youtube", None)
    store.update({"youtube": {"channel": ""}})


def access_token(store):
    sec = store.secret("youtube")
    if not sec:
        raise NeedsReconnect("YouTube is not connected")
    status, _, body = net.request("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                  urlencode({"client_id": store.settings()["youtube"]["client_id"],
                                             "client_secret": sec["client_secret"],
                                             "refresh_token": sec["refresh_token"],
                                             "grant_type": "refresh_token"}).encode())
    data = json.loads(body or b"{}")
    if status != 200:
        if data.get("error") == "invalid_grant":
            raise NeedsReconnect("YouTube sign-in expired. Click Reconnect.")
        raise RuntimeError(f"YouTube sign-in refresh failed ({status})")
    return data["access_token"]


def _channel_name(token):
    try:
        status, _, body = net.request("GET", "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                                      {"Authorization": f"Bearer {token}"})
        items = json.loads(body).get("items", []) if status == 200 else []
        return items[0]["snippet"]["title"] if items else "(no channel found)"
    except (OSError, ValueError, KeyError):
        return ""


def _offset(hdrs):
    rng = hdrs.get("range")
    return int(rng.split("-")[1]) + 1 if rng else 0


def upload(store, path, progress=lambda pct: None):
    """Upload as Private. Returns the video link."""
    token = access_token(store)
    size = path.stat().st_size
    meta = {"snippet": {"title": path.stem[:100].replace("<", "").replace(">", "") or "Untitled",
                        "categoryId": "22"},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
    status, hdrs, body = net.request(
        "POST", "https://www.googleapis.com/upload/youtube/v3/videos?"
        + urlencode({"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "false"}),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
         "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/*"},
        json.dumps(meta).encode())
    if status == 401:
        raise NeedsReconnect("YouTube refused the sign-in. Click Reconnect.")
    if status != 200 or "location" not in hdrs:
        raise RuntimeError(f"YouTube would not start the upload ({status}): {_reason(body)}")
    session = hdrs["location"]

    offset, failures = 0, 0
    with open(path, "rb") as f:
        while True:
            f.seek(offset)
            data = f.read(CHUNK)
            try:
                status, hdrs, body = net.request("PUT", session, {
                    "Authorization": f"Bearer {token}", "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}"}, data)
            except OSError:
                status = 0
            if status in (200, 201):
                progress(100)
                return f"https://youtu.be/{json.loads(body)['id']}"
            if status == 308:
                offset, failures = _offset(hdrs), 0
                progress(offset * 100 // max(size, 1))
                continue
            if 400 <= status < 500 and status not in (401, 408, 429):
                raise RuntimeError(f"YouTube rejected the upload ({status}): {_reason(body)}")
            failures += 1
            if failures > 6:
                raise RuntimeError(f"YouTube upload kept failing (last status {status})")
            _sleep(min(2 ** failures, 60))
            if status == 401:
                token = access_token(store)
            try:  # ask how much arrived, then continue from there
                status, hdrs, body = net.request("PUT", session, {"Authorization": f"Bearer {token}",
                                                 "Content-Length": "0", "Content-Range": f"bytes */{size}"}, b"")
            except OSError:
                continue
            if status in (200, 201):
                return f"https://youtu.be/{json.loads(body)['id']}"
            if status == 308:
                offset = _offset(hdrs)


def _reason(body):
    try:
        err = json.loads(body).get("error", {})
        return (err.get("errors") or [{}])[0].get("reason") or err.get("message") or ""
    except (ValueError, AttributeError):
        return body[:120].decode(errors="replace")
