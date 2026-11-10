"""Frame.io (V4, Adobe sign-in) upload for watch_and_upload.py.

  python3 watch_and_upload.py frameio-auth   # once: sign in with Adobe, pick the project

Each video is uploaded into the chosen Frame.io project folder, then a public
share link (https://f.io/...) is made for it. If the share link can't be made,
the normal Frame.io link (for people in your Frame.io team) is used instead.
"""

import http.client, json, os, secrets, subprocess, sys, time
from urllib.parse import parse_qs, urlencode, urlsplit

# ---------------------------------------------------------------- settings ---
FRAMEIO_CLIENT_ID = os.environ.get("FRAMEIO_CLIENT_ID", "PASTE-ADOBE-CLIENT-ID")
REDIRECT_URI      = "https://localhost"      # must match the Adobe credential
MAKE_SHARE_LINK   = True                      # False = only the team link
# -----------------------------------------------------------------------------

IMS       = "https://ims-na1.adobelogin.com/ims"
API       = "https://api.frame.io/v4"
SCOPES    = "openid,AdobeID,offline_access,email,profile,additional_info.roles"
KEYCHAIN  = ["-s", "simple-frameio-uploader", "-a", "frameio"]


class SignInExpired(Exception):
    pass


def configured():
    return not FRAMEIO_CLIENT_ID.startswith("PASTE-")


# ------------------------------------------------------------------- http ---
def request(method, url, headers=None, body=None):
    u = urlsplit(url)
    conn = http.client.HTTPSConnection(u.hostname, timeout=600)
    try:
        conn.request(method, u.path + ("?" + u.query if u.query else ""), body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conn.close()


# --------------------------------------------------------------- keychain ---
def _save(data):
    line = " ".join(["add-generic-password", "-U", *KEYCHAIN, "-w", "'" + json.dumps(data) + "'"])
    subprocess.run(["security", "-i"], input=line + "\n", text=True, check=True, capture_output=True)


def _load():
    r = subprocess.run(["security", "find-generic-password", *KEYCHAIN, "-w"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SignInExpired()
    return json.loads(r.stdout.strip())


# ------------------------------------------------------------------- auth ---
def _token(form):
    status, _, body = request("POST", f"{IMS}/token/v3",
                              {"Content-Type": "application/x-www-form-urlencoded"}, urlencode(form).encode())
    data = json.loads(body or b"{}")
    if status != 200:
        if data.get("error") in ("invalid_grant", "invalid_token", "invalid_refresh_token"):
            raise SignInExpired()
        raise RuntimeError(f"Adobe sign-in failed ({status}): {data.get('error', '')}")
    return data


def access_token():
    saved = _load()
    data = _token({"grant_type": "refresh_token", "client_id": FRAMEIO_CLIENT_ID,
                   "client_secret": saved["client_secret"], "refresh_token": saved["refresh_token"]})
    if data.get("refresh_token") and data["refresh_token"] != saved["refresh_token"]:
        saved["refresh_token"] = data["refresh_token"]   # Adobe may rotate it
        _save(saved)
    return data["access_token"], saved


def api(method, path, token, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(5):
        try:
            status, _, raw = request(method, API + path, hdrs, body)
        except OSError:
            status, raw = 0, b""
        if status == 429 or status == 0 or status >= 500:
            time.sleep(2 ** attempt * 2)
            continue
        if status == 401:
            raise SignInExpired()
        if status >= 400:
            raise RuntimeError(f"Frame.io {method} {path.split('?')[0]} failed ({status}): {raw[:200]!r}")
        return json.loads(raw or b"{}")
    raise RuntimeError(f"Frame.io {method} {path.split('?')[0]} kept failing")


def _pick(items, what, label=lambda x: x.get("name", x.get("id"))):
    if not items:
        sys.exit(f"No {what} found on this Frame.io login.")
    if len(items) == 1:
        print(f"{what.capitalize()}: {label(items[0])}")
        return items[0]
    for i, it in enumerate(items, 1):
        print(f"  {i}. {label(it)}")
    while True:
        n = input(f"Pick a {what} (number): ").strip()
        if n.isdigit() and 1 <= int(n) <= len(items):
            return items[int(n) - 1]


def sign_in():
    if not configured():
        sys.exit("Set FRAMEIO_CLIENT_ID at the top of frameio.py first.")
    secret = input("Paste the Adobe client secret: ").strip()
    state = secrets.token_urlsafe(16)
    url = f"{IMS}/authorize/v2?" + urlencode({
        "client_id": FRAMEIO_CLIENT_ID, "redirect_uri": REDIRECT_URI, "scope": SCOPES,
        "response_type": "code", "state": state})
    print("\n1. This link opens in your browser. Sign in with the Adobe ID you use for Frame.io:\n\n   "
          + url + "\n\n2. Afterwards the browser shows an error page at 'localhost'. That's expected.\n"
          "   Copy the WHOLE address from the address bar and paste it here.\n")
    import webbrowser
    webbrowser.open(url)
    back = input("Paste the address: ").strip()
    q = {k: v[0] for k, v in parse_qs(urlsplit(back).query).items()}
    if q.get("state") != state or "code" not in q:
        sys.exit("That address doesn't contain a sign-in code. Try again.")
    data = _token({"grant_type": "authorization_code", "client_id": FRAMEIO_CLIENT_ID,
                   "client_secret": secret, "code": q["code"]})
    if "refresh_token" not in data:
        sys.exit("Adobe did not return a refresh token (is 'offline_access' allowed on the credential?).")
    token = data["access_token"]

    account = _pick(api("GET", "/accounts", token).get("data", []), "account",
                    lambda a: a.get("display_name") or a.get("name") or a["id"])
    workspace = _pick(api("GET", f"/accounts/{account['id']}/workspaces", token).get("data", []), "workspace")
    project = _pick(api("GET", f"/accounts/{account['id']}/workspaces/{workspace['id']}/projects", token)
                    .get("data", []), "project")
    _save({"refresh_token": data["refresh_token"], "client_secret": secret, "account_id": account["id"],
           "project_id": project["id"], "folder_id": project["root_folder_id"],
           "project_name": project.get("name", "")})
    print(f"\nSigned in. Videos will go to Frame.io project \"{project.get('name')}\".")


# ----------------------------------------------------------------- upload ---
def upload(path, say=print):
    """Upload a file to the chosen Frame.io project. Returns a link to it."""
    token, cfg = access_token()
    acct = cfg["account_id"]
    size = path.stat().st_size
    created = api("POST", f"/accounts/{acct}/folders/{cfg['folder_id']}/files/local_upload", token,
                  {"data": {"name": path.name, "file_size": size}})["data"]
    file_id = created["id"]
    media_type = created.get("media_type") or "video/mp4"
    parts = created["upload_urls"]

    offset = 0
    with open(path, "rb") as f:
        for n, part in enumerate(parts, 1):
            f.seek(offset)
            chunk = f.read(part["size"])
            for attempt in range(6):
                try:
                    status, _, raw = request("PUT", part["url"], {"Content-Type": media_type,
                                                                 "x-amz-acl": "private",
                                                                 "Content-Length": str(len(chunk))}, chunk)
                except OSError:
                    status, raw = 0, b""
                if status in (200, 201, 204):
                    break
                if attempt == 5:
                    raise RuntimeError(f"Frame.io part {n} kept failing ({status})")
                time.sleep(2 ** attempt * 2)
            offset += len(chunk)
            say(f"  {path.name}: Frame.io {offset * 100 // max(size, 1)}%")

    link = created.get("view_url") or ""
    if MAKE_SHARE_LINK:
        try:
            link = _share_link(token, acct, cfg["project_id"], file_id, path.stem) or link
        except Exception as e:  # noqa: BLE001 - share link is a nice-to-have
            say(f"  Frame.io share link not made ({e}); using the team link instead")
    return link or f"https://next.frame.io/project/{cfg['project_id']}/view/{file_id}"


def _share_link(token, acct, project_id, file_id, name):
    base = f"/accounts/{acct}/projects/{project_id}/shares"
    try:
        share = api("POST", base, token, {"data": {"name": name, "access": "public",
                                                   "asset_ids": [file_id]}})["data"]
    except RuntimeError:
        # Older form: create the share, then add the file to it.
        share = api("POST", base, token, {"data": {"name": name, "access": "public"}})["data"]
        api("POST", f"/accounts/{acct}/shares/{share['id']}/assets", token, {"data": {"asset_id": file_id}})
    return share.get("short_url") or share.get("url")
