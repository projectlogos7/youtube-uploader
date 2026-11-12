"""All tests use fake YouTube / Frame.io servers. Nothing touches the network."""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import frameio, net, server, youtube
from app.store import Store
from app.watcher import Watcher


class FakeCloud:
    """Answers like Google, YouTube, Adobe IMS, Frame.io and S3."""

    def __init__(self):
        self.yt_videos, self.yt_inserts, self.fio_files, self.fio_parts, self.shares = [], [], [], [], []
        self.yt_received = self.yt_total = 0
        self.share_ok = True
        self.token_error = None
        self.fail = {}  # kind -> count of OSErrors to raise

    def _maybe_fail(self, kind):
        if self.fail.get(kind):
            self.fail[kind] -= 1
            raise OSError("connection reset")

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        headers = headers or {}
        u = urlsplit(url)
        if u.hostname == "oauth2.googleapis.com":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if self.token_error:
                return 400, {}, json.dumps({"error": self.token_error}).encode()
            out = {"access_token": "g-at"}
            if form["grant_type"] == "authorization_code":
                assert form["code_verifier"] and form["redirect_uri"] == "http://localhost:8765/"
                out["refresh_token"] = "g-rt"
            return 200, {}, json.dumps(out).encode()
        if u.hostname == "ims-na1.adobelogin.com":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if self.token_error:
                return 400, {}, json.dumps({"error": self.token_error}).encode()
            return 200, {}, json.dumps({"access_token": "a-at", "refresh_token": "a-rt"}).encode()
        if u.hostname == "www.googleapis.com":
            assert headers["Authorization"] == "Bearer g-at"
            if u.path.endswith("/channels"):
                return 200, {}, b'{"items": [{"snippet": {"title": "The Network"}}]}'
            if method == "POST":
                meta = json.loads(body)
                self.yt_inserts.append((parse_qs(u.query), meta))
                self.yt_total, self.yt_received = int(headers["X-Upload-Content-Length"]), 0
                return 200, {"location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=S"}, b""
            cr = headers["Content-Range"]
            if cr.startswith("bytes */"):
                return 308, ({"range": f"bytes=0-{self.yt_received - 1}"} if self.yt_received else {}), b""
            self._maybe_fail("yt_put")
            start, end = map(int, cr.split(" ")[1].split("/")[0].split("-"))
            assert start == self.yt_received
            self.yt_received = end + 1
            if self.yt_received == self.yt_total:
                vid = f"vid{len(self.yt_videos) + 1}"
                self.yt_videos.append(vid)
                return 200, {}, json.dumps({"id": vid}).encode()
            return 308, {"range": f"bytes=0-{end}"}, b""
        if u.hostname == "s3.example":
            self._maybe_fail("s3")
            assert headers["x-amz-acl"] == "private" and "Authorization" not in headers
            self.fio_parts.append(len(body))
            return 200, {}, b""
        if u.hostname == "api.frame.io":
            assert headers["Authorization"] == "Bearer a-at"
            p = u.path
            if p == "/v4/accounts":
                return 200, {}, b'{"data": [{"id": "acc1", "display_name": "Studio"}]}'
            if p.endswith("/workspaces"):
                return 200, {}, b'{"data": [{"id": "ws1", "name": "Main"}]}'
            if p.endswith("/projects"):
                return 200, {}, json.dumps({"data": [
                    {"id": "pr1", "name": "Episodes", "root_folder_id": "f1"},
                    {"id": "pr2", "name": "Reels", "root_folder_id": "f2"}]}).encode()
            if p.endswith("/files/local_upload"):
                d = json.loads(body)["data"]
                self.fio_files.append((p, d))
                size, n = d["file_size"], 3
                per = -(-size // n)
                urls = [{"size": min(per, size - i * per), "url": f"https://s3.example/p{i}?sig=1"} for i in range(n)]
                return 201, {}, json.dumps({"data": {"id": "file1", "media_type": "video/mp4", "upload_urls": urls,
                                                     "view_url": "https://next.frame.io/project/pr1/view/file1"}}).encode()
            if p.endswith("/shares"):
                if not self.share_ok:
                    return 422, {}, b'{"errors": "no"}'
                self.shares.append(json.loads(body)["data"])
                return 201, {}, b'{"data": {"id": "sh1", "short_url": "https://f.io/AbCd"}}'
        return 404, {}, b"{}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    cloud = FakeCloud()
    monkeypatch.setattr(net, "request", cloud)
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    monkeypatch.setattr(frameio.time, "sleep", lambda s: None)
    store = Store(tmp_path / "data")
    folder = tmp_path / "watch"
    folder.mkdir()
    clock = {"t": time.time()}
    w = Watcher(store, folder, settle=20, clock=lambda: clock["t"])

    class E:
        pass
    e = E()
    e.cloud, e.store, e.folder, e.w, e.clock = cloud, store, folder, w, clock
    return e


def connect_youtube(e):
    url = youtube.start_auth(e.store, "gid", "gsecret", "http://localhost:8765/")
    state = parse_qs(urlsplit(url).query)["state"][0]
    youtube.finish_auth(e.store, "code1", state)


def connect_frameio(e, pick=True):
    url = frameio.start_auth(e.store, "aid", "asecret")
    state = parse_qs(urlsplit(url).query)["state"][0]
    projects = frameio.finish_auth(e.store, f"https://localhost/?code=c1&state={state}")
    if pick:
        frameio.choose_project(e.store, projects[0])
    return projects


def add_file(e, name="Episode 1.mp4", size=700_000, age=100):
    p = e.folder / name
    p.write_bytes(os.urandom(size))
    t = e.clock["t"] - age
    os.utime(p, (t, t))
    return p


def settle_and_scan(e):
    e.w.scan()                    # first sight
    e.clock["t"] += 30
    e.w.scan()                    # unchanged for 30s -> upload


# ---- connectors --------------------------------------------------------------

def test_youtube_connect_stores_secret_privately(env):
    connect_youtube(env)
    assert youtube.connected(env.store)
    assert env.store.settings()["youtube"]["channel"] == "The Network"
    assert (env.store.secrets_path.stat().st_mode & 0o777) == 0o600
    assert "g-rt" not in env.store.settings_path.read_text()


def test_youtube_bad_state_rejected(env):
    youtube.start_auth(env.store, "gid", "s", "http://localhost:8765/")
    with pytest.raises(ValueError):
        youtube.finish_auth(env.store, "code", "forged")


def test_frameio_connect_lists_projects(env):
    projects = connect_frameio(env, pick=False)
    assert [p["name"] for p in projects] == ["Episodes", "Reels"]
    assert frameio.connected(env.store) and not frameio.ready(env.store)
    frameio.choose_project(env.store, projects[1])
    assert frameio.ready(env.store) and env.store.settings()["frameio"]["folder_id"] == "f2"


def test_frameio_bad_paste(env):
    frameio.start_auth(env.store, "aid", "s")
    with pytest.raises(ValueError):
        frameio.finish_auth(env.store, "https://localhost/?nothing=1")


# ---- watcher -----------------------------------------------------------------

def test_disabled_watcher_does_nothing(env):
    connect_youtube(env)
    p = add_file(env)
    env.w.interval, env.w.settle = 0.02, 0
    env.w.start()
    time.sleep(0.3)
    assert env.store.settings()["enabled"] is False
    assert env.cloud.yt_inserts == [] and p.exists() and env.w.snapshot()["last_scan"] is None


def test_youtube_only(env):
    connect_youtube(env)
    env.store.update({"enabled": True, "to_youtube": True, "to_frameio": False})
    p = add_file(env)
    settle_and_scan(env)
    q, meta = env.cloud.yt_inserts[0]
    assert meta["status"]["privacyStatus"] == "private" and q["notifySubscribers"] == ["false"]
    assert meta["snippet"]["title"] == "Episode 1"
    assert not p.exists()
    moved = env.folder / "Uploaded" / "Episode 1.mp4"
    assert moved.exists()
    assert "YouTube: https://youtu.be/vid1" in (env.folder / "Uploaded" / "Episode 1.mp4.links.txt").read_text()
    assert env.store.history()[-1]["link"] == "https://youtu.be/vid1"


def test_both_destinations(env):
    connect_youtube(env)
    connect_frameio(env)
    env.store.update({"enabled": True, "to_youtube": True, "to_frameio": True})
    add_file(env)
    settle_and_scan(env)
    assert env.cloud.yt_videos == ["vid1"]
    assert sum(env.cloud.fio_parts) == 700_000
    assert env.cloud.fio_files[0][0].endswith("/folders/f1/files/local_upload")
    links = (env.folder / "Uploaded" / "Episode 1.mp4.links.txt").read_text()
    assert "YouTube: https://youtu.be/vid1" in links and "Frame.io: https://f.io/AbCd" in links


def test_frameio_only_takes_non_video(env):
    connect_frameio(env)
    env.store.update({"enabled": True, "to_youtube": False, "to_frameio": True})
    add_file(env, "still.png", 5000)
    settle_and_scan(env)
    assert len(env.cloud.fio_files) == 1 and env.cloud.yt_inserts == []


def test_youtube_skips_non_video(env):
    connect_youtube(env)
    env.store.update({"enabled": True, "to_youtube": True, "to_frameio": False})
    p = add_file(env, "song.wav", 5000)
    settle_and_scan(env)
    assert p.exists() and env.cloud.yt_inserts == []
    assert "only takes video" in env.w.snapshot()["files"]["song.wav"]


def test_waits_while_file_is_changing(env):
    connect_youtube(env)
    env.store.update({"enabled": True})
    p = add_file(env, age=0)
    env.w.scan()
    env.clock["t"] += 30
    with open(p, "ab") as f:
        f.write(b"more")        # still being written
    env.w.scan()
    assert env.cloud.yt_inserts == [] and p.exists()
    assert "waiting" in env.w.snapshot()["files"][p.name]


def test_ignores_hidden_and_other_files(env):
    connect_youtube(env)
    env.store.update({"enabled": True})
    add_file(env, ".partial.mp4")
    add_file(env, "notes.txt")
    settle_and_scan(env)
    assert env.cloud.yt_inserts == []


def test_frameio_failure_does_not_repeat_youtube(env, monkeypatch):
    connect_youtube(env)
    connect_frameio(env)
    env.store.update({"enabled": True, "to_youtube": True, "to_frameio": True})
    p = add_file(env)
    env.cloud.fail["s3"] = 99
    settle_and_scan(env)
    assert p.exists() and env.cloud.yt_videos == ["vid1"]
    assert "retrying" in env.w.snapshot()["files"][p.name]
    env.cloud.fail["s3"] = 0
    env.clock["t"] += 3600
    env.w.scan()
    assert env.cloud.yt_videos == ["vid1"] and not p.exists()   # only Frame.io retried


def test_same_file_never_uploaded_twice(env):
    connect_youtube(env)
    env.store.update({"enabled": True})
    p = add_file(env)
    data = p.read_bytes()
    settle_and_scan(env)
    copy = env.folder / "Copy.mp4"
    copy.write_bytes(data)
    t = env.clock["t"] - 100
    os.utime(copy, (t, t))
    settle_and_scan(env)
    assert env.cloud.yt_videos == ["vid1"] and not copy.exists()


def test_network_drop_resumes(env):
    connect_youtube(env)
    env.store.update({"enabled": True})
    env.cloud.fail["yt_put"] = 1
    add_file(env)
    settle_and_scan(env)
    assert env.cloud.yt_videos == ["vid1"]


def test_share_link_falls_back_to_team_link(env):
    connect_frameio(env)
    env.cloud.share_ok = False
    env.store.update({"enabled": True, "to_youtube": False, "to_frameio": True})
    add_file(env)
    settle_and_scan(env)
    assert env.store.history()[-1]["link"] == "https://next.frame.io/project/pr1/view/file1"


def test_expired_sign_in_waits_for_reconnect(env):
    connect_youtube(env)
    env.store.update({"enabled": True})
    env.cloud.token_error = "invalid_grant"
    p = add_file(env)
    settle_and_scan(env)
    assert p.exists() and "reconnect" in env.w.snapshot()["files"][p.name]
    assert "expired" in env.store.history()[-1]["error"]
    env.cloud.token_error = None
    env.w.clear_reconnect_waits()
    env.w.scan()
    assert not p.exists()


# ---- web server --------------------------------------------------------------

@pytest.fixture(params=[True])
def web(env, request):
    handler = server.make_handler(env.store, env.w, 8765, lan=request.param)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield env, base
    httpd.shutdown()


def call(base, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=h, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_ui_and_status(web):
    env, base = web
    code, html = call(base, "/")
    assert code == 200 and b"Media Uploader" in html
    connect_youtube(env)
    code, body = call(base, "/api/status")
    s = json.loads(body)
    assert s["youtube"]["connected"] and s["youtube"]["channel"] == "The Network"
    assert b"g-rt" not in body and b"gsecret" not in body


def test_settings_toggle(web):
    env, base = web
    code, _ = call(base, "/api/settings", {"enabled": True, "to_frameio": True})
    assert code == 200
    s = env.store.settings()
    assert s["enabled"] and s["to_frameio"]


def test_google_redirect_finishes_sign_in(web):
    env, base = web
    code, body = call(base, "/api/youtube/connect", {"client_id": "gid", "client_secret": "gsecret"})
    url = json.loads(body)["url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    req = urllib.request.Request(f"{base}/?code=abc&state={state}")
    opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                              {"redirect_request": lambda *a, **k: None}))
    try:
        opener.open(req, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 303 and e.headers["Location"] == "/?ok=youtube"
    assert youtube.connected(env.store)


def test_frameio_flow_over_http(web):
    env, base = web
    _, body = call(base, "/api/frameio/connect", {"client_id": "aid", "client_secret": "s"})
    state = parse_qs(urlsplit(json.loads(body)["url"]).query)["state"][0]
    code, body = call(base, "/api/frameio/finish", {"address": f"https://localhost/?code=x&state={state}"})
    assert code == 200 and len(json.loads(body)["projects"]) == 2
    code, _ = call(base, "/api/frameio/project", {"project_id": "pr2"})
    assert code == 200 and env.store.settings()["frameio"]["project_name"] == "Reels"


def test_rejects_other_sites(web):
    env, base = web
    code, _ = call(base, "/api/settings", {"enabled": True}, {"Origin": "https://evil.example"})
    assert code == 403
    code, _ = send_file(base, "a.mp4", b"x", extra={"Origin": "https://evil.example"})
    assert code == 403
    code, _ = call(base, "/api/settings", {"enabled": True}, {"Host": "evil.example"})
    assert code == 403
    req = urllib.request.Request(base + "/api/settings", data=b"enabled=1",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 415
    assert env.store.settings()["enabled"] is False


def test_errors_are_readable(web):
    env, base = web
    code, body = call(base, "/api/youtube/connect", {"client_id": "", "client_secret": ""})
    assert code == 400 and "client ID" in json.loads(body)["error"]


# ---- network access flag -------------------------------------------------------

def test_host_rules():
    assert server.host_allowed("localhost", lan=False)
    assert not server.host_allowed("192.168.1.20", lan=False)
    assert server.host_allowed("192.168.1.20", lan=True)
    assert server.host_allowed("studio-mac.local", lan=True)
    assert not server.host_allowed("evil.example", lan=True)      # DNS-rebinding names refused


@pytest.mark.parametrize("web", [False], indirect=True)
def test_local_only_refuses_network_address(web):
    env, base = web
    code, _ = call(base, "/api/status", headers={"Host": "192.168.1.20:8765"})
    assert code == 403
    code, _ = call(base, "/api/status")
    assert code == 200


# ---- files from other computers / the page ---------------------------------------

def send_file(base, name, data, cid="", extra=None):
    h = {"X-Media-Uploader": "1", "Content-Type": "application/octet-stream", **(extra or {})}
    req = urllib.request.Request(f"{base}/api/incoming?name={urllib.parse.quote(name)}&computer={cid}",
                                 data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_dropped_file_lands_in_folder_and_uploads(web):
    import urllib.parse  # noqa: F401
    env, base = web
    connect_youtube(env)
    env.store.update({"enabled": True})
    code, body = send_file(base, "Clip.mov", b"x" * 5000)
    assert code == 200 and (env.folder / "Clip.mov").exists()
    env.w.scan()
    env.clock["t"] += 1
    env.w.scan()
    assert env.cloud.yt_videos == ["vid1"]
    assert env.store.history()[-1]["source"] == "Web page"


def test_incoming_rejects_bad_files(web):
    import urllib.parse  # noqa: F401
    env, base = web
    assert send_file(base, "../../etc/evil.mp4", b"x")[1]["saved_as"] == "evil.mp4"   # path stripped
    assert send_file(base, "notes.txt", b"x")[0] == 400
    assert send_file(base, ".hidden.mp4", b"x")[0] == 400
    assert send_file(base, "a.mp4", b"abc", extra={"X-Sha256": "0" * 64})[0] == 400
    req = urllib.request.Request(f"{base}/api/incoming?name=a.mp4", data=b"x", method="POST",
                                 headers={"Content-Type": "application/octet-stream"})   # no custom header
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403
    assert not (env.folder / ".incoming").exists() or not list((env.folder / ".incoming").iterdir())


def test_name_clash_is_renamed(web):
    import urllib.parse  # noqa: F401
    env, base = web
    send_file(base, "A.mp4", b"1")
    code, body = send_file(base, "A.mp4", b"2")
    assert body["saved_as"] == "A (1).mp4"


def test_computers_register_and_remove(web):
    env, base = web
    code, body = call(base, "/api/computers/heartbeat", {"id": "abcd1234", "name": "Arham MBP",
                                                          "folder": "/Users/a/Upload", "register": True})
    assert code == 200
    s = json.loads(call(base, "/api/status")[1])
    assert s["computers"][0]["name"] == "Arham MBP" and s["computers"][0]["online"]
    call(base, "/api/computers/remove", {"id": "abcd1234"})
    code, body = call(base, "/api/computers/heartbeat", {"id": "abcd1234", "name": "x"})
    assert json.loads(body) == {"removed": True}          # helper stops itself
    assert json.loads(call(base, "/api/status")[1])["computers"] == []


def test_agent_sends_file(web, tmp_path, monkeypatch):
    import importlib
    env, base = web
    agent = importlib.import_module("app.agent")
    src = tmp_path / "laptop"
    src.mkdir()
    f = src / "Interview.mp4"
    f.write_bytes(os.urandom(300_000))
    cfg = {"server": base, "id": "laptop0001", "name": "Laptop", "folder": str(src)}
    call(base, "/api/computers/heartbeat", {"id": "laptop0001", "name": "Laptop", "register": True})
    agent.send(cfg, f)
    assert (env.folder / "Interview.mp4").read_bytes() == f.read_bytes()
    assert env.w.sources["Interview.mp4"] == "Laptop"
    assert json.loads(call(base, "/api/status")[1])["computers"][0]["sent"] == 1


def test_agent_served_by_app(web):
    env, base = web
    code, body = call(base, "/agent.py")
    assert code == 200 and b"def send(" in body


def test_youtube_paste_from_other_computer(web):
    env, base = web
    _, body = call(base, "/api/youtube/connect", {"client_id": "gid", "client_secret": "gsecret"})
    state = parse_qs(urlsplit(json.loads(body)["url"]).query)["state"][0]
    code, _ = call(base, "/api/youtube/finish", {"address": f"http://localhost:8765/?state={state}&code=c&scope=x"})
    assert code == 200 and youtube.connected(env.store)
