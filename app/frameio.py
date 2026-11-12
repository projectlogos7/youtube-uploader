"""Frame.io connector (V4, Adobe sign-in): upload into a chosen project and make a share link.

Sign-in uses an Adobe "OAuth Web App" credential whose redirect URI is https://localhost.
After signing in, the browser lands on an error page at localhost; the user pastes that
address into the app, which reads the sign-in code from it.
"""

import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from . import net

IMS = "https://ims-na1.adobelogin.com/ims"
API = "https://api.frame.io/v4"
SCOPES = "openid,AdobeID,offline_access,email,profile,additional_info.roles"
REDIRECT_URI = "https://localhost"

_pending = {}


class NeedsReconnect(Exception):
    pass


def accepts(path):
    return True  # Frame.io takes video, audio, images, documents


def connected(store):
    return bool((store.secret("frameio") or {}).get("refresh_token"))


def ready(store):
    return connected(store) and bool(store.settings()["frameio"]["folder_id"])


# ---- sign-in ------------------------------------------------------------------

def start_auth(store, client_id, client_secret):
    state = secrets.token_urlsafe(16)
    _pending.clear()
    _pending[state] = {"client_id": client_id, "client_secret": client_secret}
    return f"{IMS}/authorize/v2?" + urlencode({"client_id": client_id, "redirect_uri": REDIRECT_URI,
                                               "scope": SCOPES, "response_type": "code", "state": state})


def finish_auth(store, pasted_address):
    q = {k: v[0] for k, v in parse_qs(urlsplit(pasted_address.strip()).query).items()}
    if q.get("error"):
        raise ValueError(f"Adobe sign-in was not completed: {q.get('error_description') or q['error']}")
    p = _pending.pop(q.get("state", ""), None)
    if not p or "code" not in q:
        raise ValueError("That address has no valid sign-in code. Click Connect and try again.")
    data = _token({"grant_type": "authorization_code", "client_id": p["client_id"],
                   "client_secret": p["client_secret"], "code": q["code"]})
    if "refresh_token" not in data:
        raise ValueError("Adobe did not return a refresh token. Is 'offline_access' enabled on the credential?")
    store.set_secret("frameio", {"client_secret": p["client_secret"], "refresh_token": data["refresh_token"]})
    store.update({"frameio": {"client_id": p["client_id"], "account_id": "", "project_id": "",
                              "folder_id": "", "project_name": ""}})
    projects = list_projects(store, data["access_token"])
    if len(projects) == 1:
        choose_project(store, projects[0])
    return projects


def disconnect(store):
    store.set_secret("frameio", None)
    store.update({"frameio": {"account_id": "", "project_id": "", "folder_id": "", "project_name": ""}})


def _token(form):
    try:
        status, _, body = net.request("POST", f"{IMS}/token/v3",
                                      {"Content-Type": "application/x-www-form-urlencoded"},
                                      urlencode(form).encode())
    except OSError as e:
        raise RuntimeError(f"Adobe sign-in server unreachable: {e}") from None
    data = json.loads(body or b"{}")
    if status != 200:
        if data.get("error") in ("invalid_grant", "invalid_token", "invalid_refresh_token", "invalid_client"):
            raise NeedsReconnect("Frame.io sign-in expired. Click Reconnect.")
        raise RuntimeError(f"Adobe sign-in failed ({status}): {data.get('error_description') or data.get('error')}")
    return data


def access_token(store):
    sec = store.secret("frameio")
    if not sec:
        raise NeedsReconnect("Frame.io is not connected")
    data = _token({"grant_type": "refresh_token", "client_id": store.settings()["frameio"]["client_id"],
                   "client_secret": sec["client_secret"], "refresh_token": sec["refresh_token"]})
    if data.get("refresh_token") and data["refresh_token"] != sec["refresh_token"]:
        store.set_secret("frameio", {**sec, "refresh_token": data["refresh_token"]})
    return data["access_token"]


# ---- API ----------------------------------------------------------------------

def api(method, path, token, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(5):
        try:
            status, _, raw = net.request(method, API + path, hdrs, body)
        except OSError:
            status, raw = 0, b""
        if status in (0, 429) or status >= 500:
            time.sleep(2 ** attempt * 2)
            continue
        if status == 401:
            raise NeedsReconnect("Frame.io refused the sign-in. Click Reconnect (use the Adobe ID linked to Frame.io).")
        if status >= 400:
            raise RuntimeError(f"Frame.io {method} {path.split('?')[0]} failed ({status}): {raw[:160].decode(errors='replace')}")
        return json.loads(raw or b"{}")
    raise RuntimeError(f"Frame.io {method} {path.split('?')[0]} kept failing")


def list_projects(store, token=None):
    token = token or access_token(store)
    out = []
    for acc in api("GET", "/accounts", token).get("data", []):
        acc_name = acc.get("display_name") or acc.get("name") or acc["id"]
        for ws in api("GET", f"/accounts/{acc['id']}/workspaces", token).get("data", []):
            for pr in api("GET", f"/accounts/{acc['id']}/workspaces/{ws['id']}/projects", token).get("data", []):
                out.append({"account_id": acc["id"], "project_id": pr["id"], "folder_id": pr["root_folder_id"],
                            "label": f"{pr.get('name', pr['id'])}  ({ws.get('name', '')}, {acc_name})",
                            "name": pr.get("name", pr["id"])})
    return out


def choose_project(store, project):
    store.update({"frameio": {"account_id": project["account_id"], "project_id": project["project_id"],
                              "folder_id": project["folder_id"], "project_name": project["name"]}})


# ---- upload -------------------------------------------------------------------

def upload(store, path, progress=lambda pct: None):
    """Upload into the chosen project. Returns a share link (or the team link)."""
    cfg = store.settings()["frameio"]
    if not cfg["folder_id"]:
        raise RuntimeError("Pick a Frame.io project first")
    token = access_token(store)
    size = path.stat().st_size
    created = api("POST", f"/accounts/{cfg['account_id']}/folders/{cfg['folder_id']}/files/local_upload",
                  token, {"data": {"name": path.name, "file_size": size}})["data"]
    media_type = created.get("media_type") or "application/octet-stream"

    offset = 0
    with open(path, "rb") as f:
        for n, part in enumerate(created["upload_urls"], 1):
            f.seek(offset)
            chunk = f.read(part["size"])
            for attempt in range(6):
                try:
                    status, _, _ = net.request("PUT", part["url"], {"Content-Type": media_type,
                                                                   "x-amz-acl": "private",
                                                                   "Content-Length": str(len(chunk))}, chunk)
                except OSError:
                    status = 0
                if status in (200, 201, 204):
                    break
                if attempt == 5:
                    raise RuntimeError(f"Frame.io upload part {n} kept failing ({status})")
                time.sleep(min(2 ** attempt * 2, 60))
            offset += len(chunk)
            progress(offset * 100 // max(size, 1))

    team_link = created.get("view_url") or f"https://next.frame.io/project/{cfg['project_id']}/view/{created['id']}"
    try:
        return _share_link(token, cfg, created["id"], path.stem) or team_link
    except Exception:  # noqa: BLE001 - the team link is a fine fallback
        return team_link


def _share_link(token, cfg, file_id, name):
    base = f"/accounts/{cfg['account_id']}/projects/{cfg['project_id']}/shares"
    try:
        share = api("POST", base, token, {"data": {"name": name, "access": "public", "asset_ids": [file_id]}})["data"]
    except RuntimeError:
        share = api("POST", base, token, {"data": {"name": name, "access": "public"}})["data"]
        api("POST", f"/accounts/{cfg['account_id']}/shares/{share['id']}/assets", token,
            {"data": {"asset_id": file_id}})
    return share.get("short_url") or share.get("url")
